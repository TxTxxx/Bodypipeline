# Server 隔离设计

本文解决一个具体问题：新增机器人适配可能需要修改 Server，但不能让这些修改改变现有机器人的上传、转换、任务和预览行为。

## 1. 先判断影响面

| 变更 | 默认影响 |
| --- | --- |
| `pipeline/client/adapters/*` | 目标机器人 Client |
| `pipeline/client/bridge.py` | 使用该 Bridge 的目标机器人 |
| `configs/client.<robot>.json` | 目标机器人 |
| `pipeline/server/*`（含 `providers/*`） | 该 Server 实例上的全部机器人 |
| `pipeline/shared/*` | 所有使用该 release 的服务 |
| `pipeline/web/*` | Client 与 Server 页面 |
| `environments/letools/*` | 该 Server 实例的全部转换与 Doctor |
| `configs/server*.json` | 该 Server 实例 |
| Server `state` schema 或迁移 | 该实例全部历史任务 |
| `docs/*`、`tests/*` | 不直接影响运行时，但可能代表未部署的代码变化 |

`data_root/<robot_type>/` 只能避免文件同名冲突，不能阻止新代码读取、修改或误处理旧机器人的数据。

## 2. 三种部署模式

### 2.1 Client-only

适用：

- 只新增或修改目标机器人 Adapter、Bridge、Client 配置；
- Server API、Manifest、`raw_format`、Profile、Provider 和转换分支不变；
- 目标机器人上传的数据能被现有 Server 原样处理。

要求：

- Server 不重启、不切换 release；
- 目标 Client 使用独立 release / 状态根；
- 用一个脱敏 episode 验证现有 Server 接收与分组；
- 现有机器人不需要重新上传或重建任务。

风险：Client 仍可能生成不兼容 Manifest，因此必须先用 fixture 走完 Server 校验，不能只看 Client 测试。

### 2.2 Isolated Server（默认推荐）

适用：

- 新机器人需要新的 Profile、Provider、`raw_format`、对齐策略或转换分支；
- 改动触及 `pipeline/server/`、`pipeline/shared/`、共享 Web 或 letools；
- 无法证明共享发布对所有旧机器人完全向后兼容；
- 需要不同 Viewer 会话、工具版本、端口或状态根。

要求：

| 资源 | 必须独立 |
| --- | --- |
| systemd service | 是 |
| release | 是 |
| `root` | 是 |
| `data_root` | 是，且不得与其他实例路径重叠 |
| Server 配置文件 | 是 |
| 环境文件 / 设备凭据映射 | 是 |
| `profiles` / Runtime 注册 | 是 |
| Server 端口 | 是 |
| Viewer Gateway 端口 | 是 |
| Viewer UI / data 端口 | 是 |
| 日志与 Viewer 缓存 | 是 |
| 反向代理或 SSH 隧道入口 | 是，至少逻辑隔离 |

可以共享主机 CPU、内存、磁盘和操作系统，但不能共享可写状态目录和监听端口。`letools` 环境如果代码或依赖发生变化，也应独立，避免升级时影响旧实例。

独立实例示例：

```text
bodypipeline-server-primary       :8081
bodypipeline-viewer-primary       :8082
viewer UI / data              :3100 / 8765

bodypipeline-server-newrobot  :8181
bodypipeline-viewer-newrobot  :8182
viewer UI / data              :3200 / 8865
```

目标机器人 Client 的 `server_url` 与 `viewer_origin` 指向新实例；现有机器人完全保留旧入口。

### 2.3 Shared additive（受控例外）

适用前提：

- 改动是纯增量；
- 旧机器人的设备身份、Profile、`raw_format` 和 Provider 路由不进入新分支；
- 新功能不会改变旧 API 响应、JSON schema、任务枚举和 Viewer 生命周期；
- 全机队回归已完成；
- 维护窗口和共享重启影响已获明确接受。

最低证据：

1. 现有机器人全部列入 `--verified-robot-id`；
2. 每个现有机器人至少通过健康检查、设备鉴权、上传归属、collection 分组和一个转换/预览回归；
3. 负样本证明错误 `robot_type`、未知格式和未授权设备会被拒绝；
4. 旧状态数据可被新版本读取，且不会自动迁移或重排任务；
5. 发布前记录共享 service 重启窗口；
6. 发布后逐台验证，不只验证新机器人。

阶段二已提供受信任的 Runtime / Provider 路由。`shared-additive` 至少要求：设备注册表绑定不同 `runtime_id`；每个 Runtime 只允许自己的 `raw_formats`；新增 Provider 不接管旧机器人；旧上传无 `runtime_id` 时可按 Profile hash 兼容解析；全机队回归证明旧 Runtime 行为不变。若无法证明这些条件，仍使用独立 Server。

## 3. 配置隔离

### 3.1 Server 配置

独立实例配置至少包含：

```json
{
  "root": "/srv/bodypipeline/newrobot/server",
  "data_root": "/data/bodypipeline/newrobot",
  "import_roots": ["/data/bodypipeline/newrobot/imports"],
  "profiles": {
    "newrobot_v1": {
      "profile_path": "/opt/bodypipeline/newrobot/configs/newrobot.json",
      "provider": "new_robot",
      "raw_formats": ["new_robot_timestamped_v1"],
      "accepted_capture_profile_hashes": []
    }
  },
  "default_profile": "newrobot_v1",
  "viewer_origin": "http://127.0.0.1:8182",
  "viewer_ui_port": 3200,
  "viewer_data_port": 8865
}
```

不要与旧实例共享 `root`、`data_root` 或 Viewer `cache-dir`。共享 `root` 会触发布进程锁，并把状态写在一起。

### 3.2 设备注册

共享环境中新增设备只能是追加项：

```json
{
  "robot_id": "robot-002",
  "project_id": "teleop",
  "token_env": "ROBOT_002_DEVICE_TOKEN",
  "targets": ["default"],
  "profile": "newrobot_v1"
}
```

不得：

- 修改现有 `token_env`；
- 复用现有设备密钥；
- 移动现有上传到新 `project_id`；
- 删除或重排现有目标映射；
- 用新 `robot_type` 覆盖旧 Profile 摘要。

### 3.3 Profile 与 Provider

- 新 Profile 使用新的 `id` / `version`，并注册为独立 Runtime；
- 不复用旧 Profile 文件名后直接改内容；
- 新 `raw_format` 需要版本化，并只加入新 Runtime 的允许列表；
- Provider 只能从静态注册表加载，不能由 Manifest 或环境变量动态导入；
- Provider / 对齐 / 转换参数由认证设备绑定的 Runtime 决定；
- 旧机器人的 Runtime、Profile 摘要和上传记录不应因部署新机器人而变化；
- `accepted_capture_profile_hashes` 只增加已核实的历史摘要，并优先放在对应 Runtime 下。

### 3.4 状态和数据

```text
独立实例 A: root=/srv/bodypipeline/a   data_root=/data/bodypipeline/a
独立实例 B: root=/srv/bodypipeline/b   data_root=/data/bodypipeline/b
```

只允许备份、只读校验和经过审批的离线迁移。部署流程不得：

- 清空 `state/`；
- 重建 collection；
- 重新扫描并自动提交旧上传；
- 覆盖旧任务的 `dataset_path`；
- 把历史任务重新标成新机器人类型；
- 将旧 `work/<job_id>/dataset` 移到新目录。

## 4. 共享资源风险

即使实例独立，以下资源仍可能互相影响：

| 资源 | 风险 | 措施 |
| --- | --- | --- |
| 同一 Server 磁盘 | 新实例写满磁盘 | 独立配额、水位、监控 |
| 同一 hostname | 端口冲突 | 独立端口和启动前 bind 检查 |
| 反向代理 | 路由覆盖 | 按路径/端口分入口 |
| SSH 隧道 | 本地端口冲突 | 独立隧道名和端口 |
| systemd user | unit 名冲突 | 独立 service 名 |
| letools 缓存 | 版本污染 | 独立 venv / cache |
| Viewer 端口 | 会话互相覆盖 | 独立内部端口 |

## 5. 判定原则

如果无法回答下面所有问题，选择独立 Server：

- 旧机器人会进入哪个 Provider / Profile / 转换分支？
- 新代码是否读取旧 `raw_format` 或旧 JSON？
- 旧 API 响应是否改变？
- 旧 View 会话是否会被新实例停止？
- 共享 Web 是否强制旧页面使用新行为？
- 如何证明旧上传没有被自动重新处理？
- 回滚时如何避免覆盖新写入？

独立 Server 的成本低于错误处理一台现有机器人数据链路的成本。

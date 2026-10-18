---
name: deploy-robot-adapter
description: "用于 BodyPipeline 完成机器人适配后的 Client / Server 部署、灰度、跨机器人回归与回滚规划。重点是隔离共享 Server 变更，防止部署新机器人时影响现有机器人；不用于尚未完成接口与数据语义适配的机器人设计工作。"
---

# 机器人适配后的安全部署

目标是把已经完成适配的机器人部署到现有多机器人环境，同时把共享 Server 变更的影响范围限制在可证明、可监控、可回滚的范围内。

“不会影响其他机器人”必须由部署边界保证，不能只靠一次重启后的抽样测试。默认使用**不可变 release + 独立 Server 实例或完整全机队回归**，禁止直接覆盖正在运行的源码和环境。

## 先确认授权与目标

1. 阅读仓库根目录 `README.md`、`docs/architecture.md`、`docs/deployment.md` 和适用的 `AGENTS.md`。
2. 检查 `git status`；保留用户已有修改，不覆盖无关工作。
3. 明确：
   - 目标机器人 `robot_id` / `robot_type`；
   - 部署目标主机、实例、服务名和访问入口；
   - 是 Client-only、独立 Server，还是共享 Server 的增量发布；
   - 哪些机器人必须保持在线。
4. 只在任务明确指定部署目标和环境时执行远端写操作。读取代码、生成发布计划或运行本地检查，不等于获准重启真实服务或操作机器人。

## 硬性隔离规则

- **不可变 release。** 每次部署解压到新的版本目录，`current` 只通过原子切换指向新版本；禁止直接在运行目录覆盖 `.py`、Web 静态文件、锁文件或虚拟环境。
- **Server 默认物理隔离。** 只要新增机器人需要修改 `pipeline/server/`、`pipeline/shared/`、共享 Web、letools 环境或 Server 状态 schema，优先为目标机器人启动独立 Server 实例：独立 service、端口、`root`、`data_root`、配置、环境文件、Viewer 端口和日志。
- **共享模式必须有证据。** 只有在改动纯增量、旧机器人不会进入新分支、全部现有机器人回归通过，并有人明确接受共享服务重启影响时，才允许更新同一个 Server 实例。
- **配置加法优先。** 新设备只追加注册项；不得重写或删除现有机器人的凭据映射、`robot_id`、`project_id`、目标、Profile 或数据路径。
- **路由必须可信。** 多种数据格式共用 Server 时，由已认证设备身份决定 Runtime / Profile / Provider；不能信任 Client 自报的 `robot_type` 选择任意代码路径。
- **当前实现边界。** Server 已支持 `profiles` 多 Runtime 路由、Provider 静态注册和每设备 Runtime 绑定；当前正式注册的 Provider 仍只有 `agilex`。新增不同 Profile/Provider 不必然要求独立实例，但必须证明新 Runtime 不会进入旧机器人分支，并通过全机队回归。
- **目录分类不是隔离。** `data_root/<robot_type>/` 只隔离文件，不隔离进程、端口、配置、Web 资源或状态 schema。
- **状态迁移 copy-on-write。** 迁移前备份；不得原地重写其他机器人的 JSON 状态。旧状态迁移工具必须可前向读取，且不能把部署后的新写入用旧备份覆盖。
- **Viewer 也要隔离。** 共享 Server 的 Viewer 是单实例会话；多个机器人同时预览时必须为独立 Server 实例配置不同 Gateway、UI 和数据端口。
- **回滚保留数据。** 回滚代码/配置可以原子完成；只有在确认部署后没有新上传、新任务或新状态写入时，才能恢复旧状态备份。

## 部署流程

### 1. 建立发布清单与影响范围

先找到适配 PR / commit，并比较发布基线与候选版本：

```bash
python3 agents/skills/deploy-robot-adapter/scripts/release_guard.py \
  --base <known-good-commit> \
  --head <candidate-commit> \
  --mode isolated-server \
  --instance <fleet-instance> \
  --target-robot-id <robot-id> \
  --fleet <fleet.json>
```

脚本只做发布前检查，不执行部署。它会：

- 检查工作树是否干净；
- 区分 Client-only 与共享 Server 影响；
- 验证独立实例的 service、端口、状态根、数据根和配置不与其他实例重叠；
- 在 `shared-additive` 模式下要求列出该实例全部已验证机器人；
- 运行 uv、Ruff、pytest、JavaScript 语法和 diff 检查；
- 输出可供部署记录引用的 JSON 报告。

先用 `--skip-checks --allow-dirty` 做静态影响分析；真正发布前必须去掉这两个参数，从干净 commit 重跑。

详细隔离策略见[Server 隔离设计](references/server-isolation.md)。

### 2. 选择部署模式

| 模式 | 适用条件 | 对其他机器人的影响 |
| --- | --- | --- |
| `client-only` | 只改目标机器人的 Adapter、Bridge 或 Client 配置 | 不应影响 Server 和其他机器人 |
| `isolated-server` | 新增 Server Provider、Profile、转换分支、共享代码或依赖 | 独立实例，物理隔离；共享主机资源除外 |
| `shared-additive` | 纯增量、向后兼容、可信路由，且全机队回归完成 | 共享进程重启和共享代码风险；必须有明确签核 |

无法证明 `shared-additive` 条件时，不要继续共享发布，改用 `isolated-server`。

### 3. 预发布验证

至少完成：

```bash
uv lock --check
uv run --locked ruff check pipeline scripts tests
uv run --locked ruff format --check pipeline scripts tests
uv run --locked pytest -q
node --check pipeline/web/app.js
git diff --check
```

另外必须覆盖：

- 新机器人的脱敏 fixture 或真实样本解析；
- Runtime 绑定、`raw_formats` 白名单、Provider 路由和设备隔离；
- 所有现有机器人 Profile、Manifest、Provider 路由和转换回归；
- 映射错误、未知格式、错误 `robot_type` 的负样本；
- 上传、collection、任务、Doctor、Viewer 的 API 兼容；
- 旧 JSON 状态能被候选版本读取；
- 候选版本不会自动扫描或处理其他机器人未完成的上传。

跨机器人验收矩阵见[现网回归矩阵](references/robot-regression.md)。

### 4. 备份与准备候选实例

部署前备份：

- 当前 release 指针和完整旧 release；
- systemd unit、反向代理和隧道配置；
- Server 配置、环境文件、`root/state`、`data_root/raw`、`data_root/work`；
- 当前 Profile、letools 环境和锁文件；
- 现有机器人设备注册与目标映射。

对新实例预创建独立的：

```text
release 根
state root
data_root
import_roots
profiles / Runtime 绑定
service 名
server / viewer / viewer-ui / viewer-data 端口
日志与 Viewer 缓存目录
```

所有资源必须与现有实例不重叠。端口在启动前用只读 bind 检查。

### 5. 金丝雀

先只让目标机器人 Client 指向候选 Server：

1. 启动候选实例，检查 `/healthz`、配置加载、Profile、设备鉴权和工具环境。
2. 用目标机器人做一次受控采集 / 上传 / 转换 / Doctor / Viewer 验证；没有授权时使用脱敏 fixture，不操作真机。
3. 同时检查现有机器人：
   - 原 Server 仍健康；
   - 现有设备认证和上传入口正常；
   - 现有 collection、任务、报告和 Viewer 状态没有被改写；
   - 原 Client / Bridge 没有指向候选实例。
4. 保存候选实例日志、任务 ID、数据集路径和验收结果。

没有完成金丝雀，不做共享切换。

### 6. 原子切换与观察

对独立实例，直接启用新 service 或切换新实例的 `current`，不要触碰其他 service。

对已明确批准的共享模式：

```text
冻结新上传窗口
→ 原子切换 current
→ 重启共享 Server / Viewer
→ 先验证目标机器人
→ 再逐台验证全部现有机器人
→ 观察错误率、任务队列、上传与预览
```

共享重启必然影响所有连接；必须提前说明维护窗口。若现有机器人验证失败，立即回滚，不要继续排查式修改线上 release。

### 7. 回滚

优先回滚 release 指针、service unit 和配置，不先恢复数据：

```bash
ln -s "$RELEASES/$KNOWN_GOOD" "$BASE/current.next"
mv -Tf "$BASE/current.next" "$BASE/current"
systemctl --user restart "$SERVICE"
```

独立实例可以停止新 service，并将目标 Client 的 `server_url` 指回原环境。

只有在确认没有新数据写入时才恢复状态备份。部署后已产生新上传或任务时，先保留新状态，再做前向修复或离线合并；禁止直接用旧备份覆盖。

完整命令、观察项和回滚顺序见[发布与回滚 Runbook](references/release-runbook.md)。

### 8. 留下部署记录

复制[部署记录模板](assets/deployment-record.template.md)，记录：

- commit、release ID、部署时间和操作者；
- 部署模式和影响范围；
- 目标实例与机器人；
- 改动文件与兼容性判断；
- 自动化、金丝雀、现有机器人回归结果；
- 备份位置；
- 回滚命令和回滚决策点；
- 未验证项、共享资源风险和最终签核。

## 禁止事项

- 禁止 `git pull` / 覆盖式同步到正在运行的 release。
- 禁止在共享 `root` 上再启动一个 Server 写进程；`flock` 会阻止或造成不可用，不能用它当多实例隔离。
- 禁止修改共享 Runtime/Profile 后让所有机器人继续上报；新增机器人应追加独立 Runtime 和设备绑定。
- 禁止把新机器人 `robot_type` 只加进目录或页面筛选，就宣称 Server 已支持。
- 禁止删除、重写或“修复”其他机器人的历史上传、任务、报告和原始数据。
- 禁止在真实共享发布中跳过现有机器人回归。
- 禁止用 Viewer 能打开、Doctor PASS 或任务 `COMPLETED` 代替多机器人隔离验证。

## 技能资源

- [Server 隔离设计](references/server-isolation.md)：三种部署模式的边界和资源配置。
- [发布与回滚 Runbook](references/release-runbook.md)：不可变 release、切换、观察和回滚命令。
- [现网回归矩阵](references/robot-regression.md)：Client、Upload、Server、转换、Doctor、Viewer 的跨机器人验证。
- [部署记录模板](assets/deployment-record.template.md)：发布证据和签核。
- [机队清单示例](assets/fleet.example.json)：供 `release_guard.py` 校验的实例结构。

# 机器人适配部署记录

## 1. 发布信息

| 项目 | 值 |
| --- | --- |
| 机器人 / 操作方式 | |
| robot_id / robot_type | |
| 适配 PR / commit | |
| 已知良好基线 | |
| 候选 release commit | |
| release 目录 | |
| 部署时间 / 时区 | |
| 操作者 | |
| 部署模式 | `client-only` / `isolated-server` / `shared-additive` |

## 2. 影响范围

- 目标 Server 实例：
- 目标机器人：
- 同一实例中的其他机器人：
- 是否重启共享服务：
- 是否修改共享 Web / letools / 状态 schema：
- 受影响的数据根和状态根：

## 3. 改动清单

```text
<git diff --name-status BASE..HEAD>
```

兼容性结论：

- [ ] 旧 Profile / `raw_format` 不进入新分支
- [ ] 旧 API、任务状态和 JSON schema 向后兼容
- [ ] 新设备注册为追加项
- [ ] 新实例 root / data_root / service / 端口不重叠
- [ ] 未改写其他机器人的数据或任务

## 4. 预发布验证

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| `uv lock --check` | | |
| Ruff check / format | | |
| pytest | | |
| `node --check pipeline/web/app.js` | | |
| `git diff --check` | | |
| 新机器人 fixture / 真实样本 | | |
| 全机队回归 | | |
| 旧状态读取 | | |

`release_guard.py` 报告：

```text
<path or JSON>
```

## 5. 备份

| 内容 | 路径 / 标识 |
| --- | --- |
| 旧 release 指针 | |
| 旧 release 目录 | |
| Server 配置与环境 | |
| systemd / 隧道 / 代理 | |
| `root/state` | |
| `data_root/raw` | |
| `data_root/work` | |
| letools 环境 / lock | |

## 6. 金丝雀结果

- 启动时间：
- 健康检查：
- 设备鉴权：
- 上传 ID：
- collection ID：
- job ID：
- dataset path：
- Doctor / Viewer：
- 新实例日志位置：

## 7. 现有机器人回归

| robot_id | 实例 | 健康 | 上传鉴权 | 任务列表 | Viewer | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| | | | | | | |

部署前后计数快照：

| 指标 | 部署前 | 部署后 |
| --- | ---: | ---: |
| uploads | | |
| collections | | |
| queued jobs | | |
| running jobs | | |
| completed / quarantined | | |
| active viewer session | | |

## 8. 切换与回滚

切换命令：

```bash
<command>
```

回滚 release：

```bash
<command>
```

回滚实例 / Client 地址：

```bash
<command>
```

数据回滚边界：

- [ ] 部署后没有新上传
- [ ] 部署后没有新任务
- [ ] 已确认可以恢复旧 state
- [ ] 如有新写入，只做前向修复，不覆盖 state

## 9. 观察与结论

- 观察窗口：
- 错误日志：
- 磁盘水位：
- 旧机器人异常：
- 未验证项：
- 最终结论：`success` / `rolled-back` / `partial`

## 10. 签核

- 适配负责人：
- 部署负责人：
- 数据 / 机器人负责人：
- 日期：

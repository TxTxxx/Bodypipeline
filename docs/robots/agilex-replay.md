# AgileX ALOHA 真机回放

回放已集成在机器人 Client：采集记录中保留成功的 episode 提供“回放”入口，侧栏也可进入 `/#replay`。页面按数据集列出已保留的 episodes，另有本地 HDF5 文件浏览。后台复用机器人现有 ALOHA 工具，管理准备数据、进程、进度、日志和停止。

回放页面统一位于 Client 的 `/#replay`，与“采集工作台”共享侧栏、顶部和登录。当前机器人由 Client 配置确定，无需在 Server 注册回放目标。Server 不再展示回放入口或提供 `/api/replay/clients/*` 转发接口；旧 `replay_clients` 配置会被忽略，可移除。

本地演示访问 `http://127.0.0.1:8080/#replay`。现场机器人测试 Client 访问 `http://127.0.0.1:18080/#replay`（机器人本机，或已建立 SSH 转发的电脑）。旧 `/replay?capture=<id>` 链接会跳转到 Client 工作台并选中对应记录，不自动启动回放。

浏览器直接请求当前 Client 的 `/api/replay/*`；轨迹从机器人本地读取、适配和执行。已上传文件仍保留在机器人上，可继续回放；仅存在于 Server 的文件不会自动下载到机器人。

## 使用方式

1. 在采集工作台录制并保留 episode；无需等待上传完成。
2. 点击这条记录的“回放”，会自动选中该 episode；也可在回放页选择数据集与 episode。
3. 点击开始；平台自动准备工具需要的文件，随后显示发送进度。停止按钮结束当前准备/回放任务。
4. 刷新页面只查询状态，不自动重新执行。

本地 `scripts/demo.py` 使用 Mock 采集和 `simulated` 回放 Provider。页面明确显示“本地模拟回放”，只在本机适配文件并模拟帧输出，不导入 ROS、不连接机器人。

真机 Client 需要显式启用 `agilex_tool`。已存在的部署配置无需替换，只添加下面字段（完整示例见 `configs/client.agilex-aloha.replay.json`）：

```json
{
  "replay": {
    "enabled": true,
    "provider": "agilex_tool",
    "trajectory_source": "state",
    "launcher": "/home/agilex/Data_Conversion_app/scripts/run_data_tool.sh",
    "robot_home": "/home/agilex",
    "tool_script": "/home/agilex/data_ros/src/data_tools/scripts/data_publish.py",
    "roots": {"aloha-episodes": "/home/agilex/data_episode"}
  }
}
```

`robot_home`、`tool_script` 可省略，默认值如上。平台采集根目录自动加入回放来源，`roots` 用于额外的历史数据。未配置回放工具时仍可浏览和识别平台数据，但不启动回放。

`trajectory_source` 显式选择轨迹来源：`state` 使用 `puppetLeft/puppetRight` 实测关节轨迹；`action` 使用 `masterLeft/masterRight` 指令轨迹（未配置时保持旧默认 `action`）。当前松灵现场部署已选择 `state`。来源缺失会报错，不自动回退。原始训练数据的 action/state 语义和内容保持不变。

## 数据格式与工具复用

| 输入 | 处理 |
| --- | --- |
| 原工具 ALOHA HDF5 | `action` 直接使用原命令；`state` 提取 puppet 轨迹并映射为工具输入 |
| Bodypipeline 原始 `episode.hdf5` | 按配置读取左右 `master` 或 `puppet` 时间戳与 position，按 Profile 的输出帧率在公共区间取因果前置样本，生成工具格式 |
| Bodypipeline 对齐 HDF5 | 按配置使用 `action` 或 `observation.state` 的左七维/右七维，沿用文件 `fps` |
| LeRobot Parquet / 其他机器人 | 当前不直接支持；需新增输入格式或机器人 Provider |

格式适配只改变数组布局与时间采样，不裁剪关节值、不改变夹爪值、不换算单位、不插值运动轨迹。原文件不修改，适配缓存位于 Client `replay-cache/<provider>/<version>/<source-sha256>-<fps>-<action或state>-v2/episode.hdf5`。缓存只保存控制目标，不复制相机视频。`state` 缓存把 puppet 数值写入原工具要求的 `masterLeft/masterRight` 字段，再按既有映射发布到左右控制话题；缓存属性记录真实来源，左右臂顺序与单位保持不变，旧 master 缓存不会被复用。准备失败时显示原因，不启动外部工具。

2026-10-19 只读核实的原工具链：

```text
/home/agilex/Desktop/ALOHA-Data-Tools.desktop
  → /home/agilex/Data_Conversion_app/launch_aloha_data_tools.sh
  → aloha_data_tools/core.py:build_replay_command()
  → /home/agilex/Data_Conversion_app/scripts/run_data_tool.sh
  → /home/agilex/data_ros/src/data_tools/scripts/data_publish.py
```

对于原工具 HDF5，使用完全相同的命令，以参数数组执行：

```bash
/home/agilex/Data_Conversion_app/scripts/run_data_tool.sh \
  data_publish.py --datasetDir /absolute/path/to/episode.hdf5
```

其默认 30 Hz，把 `masterLeft/masterRight` 发送到 `/joint_left_states`、`/joint_right_states`，并按自身 YAML 发布相机、从臂反馈等流。引用图片的 HDF5 必须保留对应图片目录。

平台新采集包没有原 YAML 所要求的全部流，不能直接使用该 YAML。`replay_providers/run_agilex.sh` 加载相同的 ROS/data Conda 环境；`replay_providers/agilex_runner.py` 导入已安装 `data_publish.py`，向原 `RosOperator` 传入左右 master 控制流以及适配后文件，其他流为空。实际 ROS 消息发布与运动指令仍由原工具完成，没有复制或重写控制算法，也不伪造缺失的相机/末端姿态。

Client 必须在安装了工具的机器人主机上运行，机器人驱动按原工具的使用方式预先准备。此功能不自动启动驱动、不提供回零或额外关节限位机制。

## 状态与运行方式

```text
准备数据 → 回放中 → 全部帧已发送
    ↓         ↓
   已停止 / 回放失败
```

- 解析原工具的 `frame: N`。最后一帧后，按原桌面工具逻辑结束仍在 ROS spin 的进程。
- “全部帧已发送”表示发布完成，不代表测量验证机械臂到位。
- 工具异常、提前退出或 15 秒无帧输出标记失败，保留最近 100 行日志。
- 准备期间停止会取消后续启动；正常服务关闭也会结束本次工具进程。
- 停止仅针对本次进程组，使用 SIGINT，必要时 SIGTERM/SIGKILL；不停止其他驱动。
- 同一请求 ID 重试不会重复启动当前记录。
- Client 内录制与回放互斥，外部桌面工具和遥操作程序不受本服务管理。
- `replay-state.json` 记录本次任务；异常重启不自动重播。Linux 上如原 PID 与启动时间仍匹配，先在机器人上结束原工具再清除中断状态。
- Web API 沿用 Client 鉴权与 Origin 检查。

## 扩展点

- `pipeline/client/replay.py`：通用任务与进程管理，静态 Provider 注册。
- `pipeline/client/replay_providers/agilex_formats.py`：数据格式识别与适配。
- `pipeline/client/replay_providers/agilex_runner.py`：复用原 `RosOperator`，只构造输入参数。
- `pipeline/web/client-replay.js`：Client 工作台内的采集/文件选择、运行进度和日志。

新增机器人按[多机器人回放适配契约](replay-adaptation.md)实现独立 Provider，声明机器人类型、支持格式和版本，使用可信本地配置绑定。通用服务和页面不增加机器人型号分支。

## 本地验证边界

自动测试包含真实 Mock 采集封存后的回放、原始包与对齐文件映射、数值不变/源文件不变、原 Operator 加载、准备中取消、主动停止、异常退出、重复请求、采集互斥、鉴权及重启状态。模拟 Provider 仅在 mock Client 可用。它们不连接 ROS、不发送运动指令；2026-10-19 的现场 puppet 实测轨迹回放已由用户确认成功；后续界面迁移验证不自动启动真机运动。

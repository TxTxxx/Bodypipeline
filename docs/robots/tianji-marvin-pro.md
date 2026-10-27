# Tianji Marvin Pro：Apex Client 与现有 Server

此组合保留 PR #5（`9a264bdb87ffde052fc7d0c3ca5ceb35697db5cb`）的 Client：
`ApexAdapter`、MCAP packager、采集配置、Profile 和订阅式预览脚本。
Server 沿用本地已适配的 `TianjiProvider`，通过官方 `tianji-provider inspect` 和
`letools convert --source-format tianji` 处理 MCAP。

PR 的 Server worker、MCAP 转中间 HDF5 和 HDF5 mapping 转换路径未纳入此组合。
Server 安装仍使用 `scripts/install_letools.sh` 固定的 `tianji-provider 0.6.3` 和
LeTools 提交，详见 [LeTools 集成](../letools-integration.md)。Client Profile 中的
`native_provider_version: 0.7.0` 和 notes 是 PR 原始核查记录，保留用于识别既有
Client Manifest；它不选择或要求 Server 使用 0.7.0。

## 数据流和职责

```text
统一 Client 会话 API
  → ApexAdapter 调用本机 Apex recorder
  → 等待 data/video writer 均关闭
  → 复制到 Client 管理的 raw/episode0，原始录制目录保持不变
  → TianjiMCAPPackager 逐文件计算 SHA-256，保留 MCAP 字节
  → 通用上传队列 / 分块续传 / Server 回执
  → 设备绑定的 tianji_thor Runtime
  → 现有 TianjiProvider 复制至 work 并调用官方 inspect
  → LeTools 的 Tianji source provider → LeRobot v2.1 / v3.0
```

Client UI、API、会话和上传流程共用通用实现。适配器和 packager 由部署配置显式选择；
未知适配器、Apex 与 packager 配对不一致、错误机器人 Profile 均拒绝启动。
Apex 适配器仅访问 loopback HTTP 的 `status/projects/start/stop` 录制接口。
它不提供运动控制；`close()` 只关闭连接，不停止录制。

## 遥操作手柄触发

右手柄 B 当前由 Tianji 遥操作节点直接切换 Apex recorder 的开始与保存。BodyPipeline
启用 `controller_trigger` 后，前端“准备手柄采集”只创建一个显式 DRAFT；Client 后台
监视由该链路启动的 Apex 会话，并把它绑定到该 DRAFT。新版 controller 会携带
`controller-` request id；当前现场手柄链路不传 request id，因此部署配置显式启用
`accept_missing_request_id`。第二次按 B 后，Client 等待 data/video writer 都关闭，
将录制复制到受管目录，然后自动封存并上传到 H800；Server 不可达时保留本地文件并进入
上传重试队列。

右摇杆按下继续使用原手柄取消语义，并由 Client 将对应会话直接标记为 `DISCARDED`，
不会封存或上传。每次开始下一段手柄采集前，需要在前端重新准备一个 DRAFT，避免在未知
数据集或旧任务上静默追加数据。

Preview 默认在 Apex 录制期间停止 H.264 解码，空档期最多以 2 FPS 生成预览，并将单路
画面缩到 640 px。录制期间网页预览会暂停，但相机原始录制不受影响。可使用
`--no-pause-during-recording` 和 `--max-preview-fps` 调整这一取舍。

## 两端配置

| 用途 | 文件 | 契约 |
| --- | --- | --- |
| Client | `configs/client.tianji-marvin.json` | `capture_adapter=apex`、`capture_packager=tianji_mcap` |
| Client Profile | `configs/tianji-marvin-pro-v1.json` | 保留 PR 原始内容和摘要 |
| Server 示例 | `configs/server.tianji.json` | `tianji_thor` Runtime，显式绑定 `provider=tianji` |
| Server Profile | `configs/tianji-thor-v1.json` | Server 持有的关节、相机和转换参数 |

两端示例设备身份一致：`robot_id=marvin-pro-001`、`project_id=bodypipeline-tianji`。
两端必须使用一致的设备凭据；密钥由环境变量提供，不写入仓库。

Server Runtime 显式接受 `tianji_apex_mcap_v1`，同时保留既有
`tianji_mcap_v1` / `tianji_ros2_mcap_v1`。PR Client Profile 与 Server Profile 的
机器人类型、控制方式、16 维关节顺序与单位、输出帧率和四路相机 ID 一致；Server
配置只批准该 Client Profile 的精确 SHA-256。更改 Profile 后需重新审核摘要，
不按型号自动放行，也不会覆盖 Server 的转换参数。

这两个文件是配套示例。已有 Server 不需要另起实例或重新安装工具链；以后将本分支
更新到现有部署时，应将对应 Runtime 的 `raw_formats` 和
`accepted_capture_profile_hashes` 合入部署配置，并把设备注册表中的该设备绑定到
`tianji_thor`。保留已有其他机器人 Runtime、设备和数据目录配置。代码进入远端分支
本身不会更新现有服务，本次组合也不执行部署或重启。

## Client 现场配置

1. 设置可达的 `server_url`，将设备身份与 Server 注册表对应。
2. 将 `capture_options.project_id` / `storage_root` 指向专用 Apex 项目和存储目录，
   两者必须与 Apex `projects` 返回值一致；Client 需有读取录制目录及写入受管目录的权限。
3. 示例默认 `allow_recording=false`、`replay.enabled=false`。只有现场确认录制项目和
   writer 状态契约后才开启采集；该配置不启用机器人回放。
4. Client 使用项目普通 Python 环境。Apex HTTP 路径不要求 ROS2 Bridge 或
   `PIPELINE_BRIDGE_TOKEN`。预览脚本另在具有 ROS2、PyAV、OpenCV 的机器人环境运行：

   ```bash
   python scripts/tianji_preview.py --root /var/lib/bodypipeline/tianji/preview
   ```

   默认订阅 `/quad_tile/compressed_undistorted`，实际 topic 不同时通过 `--topic` 指定。
   该脚本仅订阅并输出预览 JPEG；不改变原始 MCAP，不调用 recorder 或运动接口。
   Client 会拒绝过期的源时间戳/接收时间戳。

Adapter 会在请求 start 前持久化意图，丢失确认后通过状态查询恢复，避免重复 start。
停止只针对其持有的 Apex 会话，且只有 `saved`、data/video 均为 `closed` 后才复制和封存。
已存在的目标或 `.copying` 目录不会被覆盖。上传成功后保留 Client MCAP 和 Manifest，
也保留 Apex 原始录制目录；HDF5 采集包原有的 ACK 后清理行为不变。

## 验证边界

`tests/test_tianji.py` 覆盖 PR Client 的录制归属、丢失确认恢复、writer 排空、
不可变封存、配置摘要审批及通过实际设备上传 API 的 Client → Server 回执。
可选工具链测试生成标记为 SYNTHETIC 的 MCAP，经当前 Server JobRunner、官方
Tianji provider 和 LeTools 完成转换与 deep validate；未安装工具链时该测试跳过：

```bash
PIPELINE_TEST_LETOOLS_BIN=/absolute/path/to/letools \
  python -m pytest -q tests/test_tianji.py tests/test_tianji_provider.py
```

这些测试证明软件与数据契约，不证明真机采集、现场 Apex 版本、运动语义或相机曝光同步
已经验收。现场仍需用实际录制样本检查 action 来源、时间戳、夹爪标定、四路相机和
力矩窗口，并核对本机 Apex 接口响应与适配器约定。

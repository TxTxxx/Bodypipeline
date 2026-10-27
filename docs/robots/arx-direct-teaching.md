# ARX direct-teaching 接入

`ARXDirectTeachingProvider`（`arx_direct_teaching`）接受 grus 采集器的
`arx_direct_teaching_hdf5_v1`。它与历史 `aloha_hdf5` Runtime 分开绑定，
不按目录名猜测格式，不把未知 schema 当成兼容文件。

Server Runtime 使用 `configs/arx-x5-direct-teaching-v1.json`，机器人类型统一为
现有 `ARX_X5`。设备注册项明确绑定 `robot_id=arx-x5-001`、`project_id=teleop`
和 `runtime_id=arx_direct_teaching`。
Profile 从 grus 当前文件整理而来，机器人类型归一为 `ARX_X5`，摘要为
`014b72def6849c51ce2d68f7949e0189a36d285826b1edf2b67abbcfe448378b`。

## 正式上传

Client 应使用既有 `/device/uploads` HTTP 分块上传接口：创建、查询偏移、上传、
完成。必须移除 `capture.upload_ssh` 才会进入此分支；仅修改 `server_url` 无效。
`episode.hdf5` 是唯一包文件，Manifest 携带采集 Profile 和 SHA256。

Server 在 `COMMITTED` 前校验文件摘要、schema、自包含 HDF5、全部 JPEG、
逐维字段、实测轨迹 action、时间戳、会话/设备/项目/Profile/任务元数据。
相同会话可断点续传、重复提交；登记成功后出现在工作台。
SSH/rsync 到共享目录的历史文件不会因此自动登记。

## 转换与验收

raw_first 文件按参考相机精确源时间戳建立均匀时间网格，使用有界最近完整快照；
其他同 schema 文件使用记录的单调时钟。保留源索引并报告帧复用，超长采集间隙、
超出帧对齐或跨传感器时间差容差会阻止转换。原文件不修改。
对齐 HDF5 复用通用 LeTools preset，输出 LeRobot v2.1 / v3.0。

`action` 的含义是同帧实测关节位置作为示教目标，不是独立控制命令。
Profile 保留 `semantics_approved=false`、`clock_verified=false`，夹爪单位仍待
现场标定。可上传登记；真实转换任务保持验收阻塞，不能靠接入代码宣称已通过。
此 Provider 不开放未经标定的夹爪闭合度修正。

## h800 部署与 grus 配置

h800 服务端入口是 `http://100.64.0.71:8081`。设备密钥通过独立环境变量
`PIPELINE_ARX_DEVICE_TOKEN` 绑定；Client 使用对应值作为 `PIPELINE_DEVICE_TOKEN`。
密钥保存在部署账号私有 handover 目录，不写入仓库或日志。

grus 由配置/服务所属账号修改并重启 Client：设置 `server_url`、设备密钥，
移除 `capture.upload_ssh`，保持当前设备身份和采集 Profile。历史 ACKED 的 SSH
任务不会自动重传，需单独明确选择重传或导入，避免重复和错误身份登记。

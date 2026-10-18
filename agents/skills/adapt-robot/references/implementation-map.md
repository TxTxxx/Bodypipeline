# 实现定位与适配契约

本文对应当前 BodyPipeline 仓库；在其他项目或独立安装环境中，必须以目标代码重新核查。先定位下列接缝，再决定是否新增 Adapter、Profile 或 Provider：

```text
Client 配置/Adapter 选择
→ CaptureAdapter 契约
→ 原始格式封存与 Manifest
→ Server 接收校验
→ Provider staging/对齐
→ letools 转换参数
→ 转换结构检查 / Doctor
→ Visualizer
```

当前实现主要针对 AgileX 三相机、双臂 7+7 维主从臂数据。这些限制是待扩展的现状，不是技能适用范围。完整文件矩阵见仓库根目录 `docs/robots/new-robot-adaptation.md`。

## 代码导航

| 位置 | 当前实现 / 适配检查点 |
|---|---|
| `pipeline/client/adapters/base.py` | `CaptureAdapter` Protocol；没有自动发现或插件加载 |
| `pipeline/client/adapters/ros2.py` | Client 到本机 Bridge 的 HTTP 翻译；Bridge 响应契约不变时可复用 |
| `pipeline/client/bridge.py` | 绑定 `data_msgs`、`Image`、`JointState`、`PoseStamped`；service 字段、status 和 worker 识别有 AgileX 特例 |
| `pipeline/client/service.py` | 只选择 Mock/Ros2；会话状态机、丢弃、封存与 Manifest；固定 `raw_format`、`control_mode` 和 AgileX JPEG 目录检查 |
| `pipeline/client/adapters/mock.py` | 输出 AgileX 目录、每流 7 维；新布局需要对应 Fake/fixture，不能拿它证明真实适配 |
| `pipeline/client/app.py`、`pipeline/web/app.js` | 相机卡片尽量来自 Profile；核查相机 ID、JPEG 预览和新增采集字段，不在前端按型号分支 |
| `pipeline/client/upload.py` | 分块、offset、摘要与重试可复用；新采集协议不应重写上传层 |
| `pipeline/shared/config.py` | `Settings` 字段、环境变量和启动校验；支持旧单 `profile_path` 与新 `profiles` Runtime 注册表，Profile 只接受 teleoperation |
| `pipeline/shared/hdf5.py` | AgileX 原始 / 对齐 HDF5 schema、单文件打包、元数据读取和 HDF5 定位；新机器人应新增版本化 schema，不能复用 AgileX 名称 |
| `pipeline/shared/collections.py` | 数据集分组键包含 robot_type、Profile、capture_mode、raw_format、target、prefix |
| `pipeline/server/ingest.py` | 身份/路径/摘要/配额校验；上传时按认证设备绑定 Runtime 校验 `raw_formats`、`robot_type` 与 Profile 摘要 |
| `pipeline/server/storage.py` | `data_root/<robot_type>/...` 目录路由；目录分类不等于转换适配完成 |
| `pipeline/shared/runtime.py` | `RobotRuntime`：Runtime ID、Profile、Provider、允许的 `raw_formats` 和兼容 Profile 摘要 |
| `pipeline/server/providers/registry.py` | Provider 静态注册表与 `RuntimeCatalog`；设备/上传/任务只从可信 Server 状态解析 Runtime，不能动态 import |
| `pipeline/server/providers/agilex.py` | AgileX Provider：Profile schema、对齐、质量、letools 参数和特征名修补的边界 |
| `pipeline/server/alignment.py` | `prepare_agilex_hdf5()` 直接从上传 HDF5 生成 `aligned/episodeN.hdf5`；`prepare_agilex()` 仅兼容旧时间戳目录，均由 AgileX Provider 调用 |
| `pipeline/server/jobs.py` | 通用 JobRunner：解析 Runtime、调用 Provider、批量快照、逐 episode 诊断、全量校验、去重、转换、Doctor 与恢复；不按机器人型号写条件分支 |
| `pipeline/server/quality.py` | Provider 声明期望维度；通用函数校验 names/units 一致性，不执行关节/动作软限位检查。Doctor 固定运行 11 项，显式排除 `kinematics` |
| `pipeline/server/checks.py` | 独立 deep validate / Doctor 质检记录；不重新转换，也不改变原任务或转换结果 |
| `pipeline/server/datasets.py` | 读取已生成数据集和结果路径；适配新输出结构时核查 |
| `pipeline/server/viewer.py`、`viewer_bootstrap.py` | 只读 Visualizer 生命周期、无登录代理和视频 Range；通常无需型号分支，但需验证新 feature/相机 |
| `pipeline/shared/core.py` | JSON Store、原子替换、路径约束和进程锁；不引入数据库 |
| `scripts/install_letools.sh`、`environments/letools/` | letools 固定版本与独立 uv 环境；先核查上游实际 CLI/provider 能力 |

## Client 契约

```python
class CaptureAdapter(Protocol):
    def health(self) -> dict: ...
    def start(self, session_id: str, raw_root: Path, instruction: str) -> dict: ...
    def stop(self, session_id: str) -> dict: ...
    def status(self) -> dict: ...
    def frame(self, camera_id: str) -> bytes | None: ...
    def close(self): ...
```

| 方法 | 调用方依赖与适配要求 |
|---|---|
| `health` | `ready` 决定能否开始；检查服务、必要流新鲜度、已有 recorder 和归属，不只检查进程存在 |
| `start` | 成功通常返回 `{"started": true}`。通用 `CaptureService.start()` 目前不检查该字段，因此自定义 Adapter 必须在正常返回前确认真正开始；AgileX Bridge 会等待唯一 worker 和首条 `CaptureStatus`。失败/未知必须抛异常 |
| `stop` | 至少返回 `{"stopped": true, "fail": false}`；`true` 必须表示停止写入已确认。失败状态不能在后续 stop 中被抹掉 |
| `status` | 健康监控读取 `fail` 并可触发受控停止；预览不可用不代表 recorder 已停止 |
| `frame` | 返回新鲜 JPEG；不可用返回 `None` 或错误，过期帧不能冒充实时画面 |
| `close` | 只释放 Client/Bridge 资源，不隐式调用真实 start/stop |

保留 `Ros2Adapter` 时，Bridge 路由为：

```text
GET  /health
GET  /status
GET  /frames/{camera_id}
POST /start   {session_id, raw_root, instruction}
POST /stop    {session_id}
```

Bridge 只监听 loopback，使用独立 Bearer 凭据。`raw_root` 是平台生成的会话目录；适配厂商接口时必须核查是否重复拼接 episode、能否指定目录、是否覆盖已有数据。

某些 stop 成功只表示“请求已接受”。必须依据实现确认 writer 退出、句柄关闭或完成事件，不能用固定 sleep 证明排空。AgileX 的 quit topic 本身不证明写入结束，`data_capture_worker_*` 也不是其他机器人的 owner 规则。

## Profile 与格式边界

当前 AgileX recorder 暂存结构与上传包：

```text
recorder raw/episode0/
  camera/color/{left,front,right}/<秒级时间戳>.jpg
  arm/jointState/{puppetLeft,puppetRight,masterLeft,masterRight}/<秒级时间戳>.json
        ↓ package_agilex_capture()
upload episode.hdf5
  /streams/camera/<id>/{timestamps,jpeg}
  /streams/arm/jointState/<stream>/{timestamps,position}
```

每条关节流为 7 个有限数值；state 使用从臂反馈，action 使用主臂控制目标回显。该映射仅属于已核实的 AgileX ALOHA Profile，不是所有机器人的默认值。新机器人应复用单文件 HDF5 的可靠性思路，但必须版本化 schema/`raw_format` 并明确自己的字段映射。

| Profile 内容 | 当前处理 |
|---|---|
| `id` / `version` / `robot_type` / `control_mode` | 版本化硬件与操作语义；当前只接受 teleoperation，VR 遥操作可沿用但必须区分 action schema |
| `cameras` / `service` / `status_topic` / `joint_topics` / `pose_topics` | 接口地址可配置；消息类型和字段映射不可仅靠 Profile 改变 |
| `state_streams` / `action_streams` | 当前固定布局；变化时同步 Provider staging、特征和 QC |
| `joint_names` / `joint_units` / `state_limits` / `action_limits` | 名称与单位是转换契约；限位字段仅为可选说明，不要求、不校验、不阻断转换 |
| `output_fps` / `max_camera_skew_ms` / `max_action_age_ms` / `minimum_frames` | 必须来自采样、时钟和任务质量证据，不能为通过样本而放宽 |
| `semantics_approved` / `clock_verified` | 缺证据保持 false；批准必须有可复核来源 |

同型号、同操作语义设备主要检查 `robot_id`、`project_id`、`local_roots`、`server_url` 和 Server `devices`。设备注册只解决身份和目标授权，不自动实现多 Profile 路由。

异构设备或同一设备不同操作方式共用 Server 时，应由已认证设备身份绑定 Runtime，再决定允许的 Profile、`raw_formats` 和转换 Provider；不能信任 Client 自报的 `robot_type`。`accepted_capture_profile_hashes` 只接受已审核的兼容摘要，不是路由表或通用放行入口。新增机器人主要新增 `pipeline/server/providers/<robot>.py`、静态注册、Runtime 配置和测试，`jobs.py` 只处理通用编排。

## 新格式必须贯通的环节

1. `CaptureService.seal()` 与 robot packager：真实流完整性检查、版本化 `raw_format`、`control_mode`、动作 schema 版本、单文件 HDF5 和 Manifest 快照；确认停止写入后再封存。
2. `IngestService.validate_manifest()`：格式允许列表、HDF5 文件数/路径、身份、配额和摘要；旧目录格式继续兼容但不能成为新格式的默认假设。
3. Provider staging：明确输入契约，可直接基于 HDF5 mapping 对齐并输出 `aligned/episodeN.hdf5`；对齐报告记录流级误差、复用帧、缺失和拒绝原因。
4. `RuntimeCatalog` / Provider：由认证设备绑定一个 Server Runtime，再选择静态 Provider、Profile 和 letools 输入方式；先核实固定 upstream commit 的真实 CLI/provider，不假设支持任意 ROS bag、维度或动作表示。
5. 特征/QC：分别定义 state/action 的维度、名称、单位和坐标/时间语义。不要增加关节/动作软限位或 URDF 运动学门禁；限位属于控制器与安全层。
6. 血缘与质检：记录 Provider/source-format、映射/标定版本、Profile 与输入摘要；Doctor 的 WARN/FAIL、必需检查 skipped 或覆盖不足写入报告并把任务标为 QUARANTINED，不删除或迁移数据。

上传提交必须以 Server 的持久化回执为准。只有 ACK 校验成功后，Client 才可清理已经封存到 HDF5 的
recorder 临时目录 `raw/episode0/`；`episode.hdf5` 和 Manifest 必须保留。上传、提交或回执校验失败
时不得清理暂存目录。批量转换必须逐 episode 记录诊断，任一 episode 失败就阻止正式数据集生成，
不能用“跳过坏 episode”制造看似成功的部分数据集。

当前对齐采用相机有界最近邻和关节因果有界保持。增量、速度、位姿、离散按钮和事件不能未经验证套用该策略；也不通过放宽误差掩盖缺样。

## 禁止软限位门禁

数据转换不是机器人安全控制器。以下做法均禁止：

- 要求 Profile 必须提供 `state_limits` / `action_limits`；
- 将 state/action 数值与标称关节范围比较后使转换失败；
- 因超出名义范围而自动裁剪、删除、重采样或丢弃样本；
- 在 Doctor 中包含 `kinematics`（URDF 位置/速度限位）检查；
- 用“放宽限位”替代修复真实的时间、编码、单位或动作语义问题。

限位应由机器人控制器、运动规划器、急停/安全 PLC 或部署侧独立监控负责。转换链应保留原始数值和来源，只做结构、维度、有限数值、时间、视频完整性和任务语义可解释性检查。

新增 HTTP/SDK 等真实后端时，检查 `jobs.py` 中以 `capture_mode == "ros2"` 触发的审批、QC 和特征更新分支。应显式建模真实/模拟来源，不能因新枚举让真实数据跳过转换准入，也不能把真实数据标成 mock。

当前缺少通用的操作方式、命令边界、坐标映射和事件 schema。需要时同步扩展 Profile、Manifest、封存、接收、血缘、转换、特征导出和测试。详见[操作方式与动作语义](control-semantics.md)。

## 选择测试样本

- 设备/授权：`tests/test_server_api.py`；覆盖设备身份、目标归属和旧配置。
- 采集协议：`tests/test_client.py`、`tests/test_upload_agent.py`；Fake 的超时和失败应映射真实接口。
- 采集延迟/状态：`tests/test_capture_latency.py`；覆盖预览契约、后台封存和命令响应。
- 新格式：用小型可复现样本贯通 `tests/test_ingest.py`、`tests/test_processing.py`；旧 Mock 不能证明新格式可用。
- 数据分组/存储：`tests/test_collections.py`、`tests/test_data_storage.py`；避免不兼容数据混组和路径逃逸。
- 新维度/动作：`tests/test_quality.py` 和新 Provider 测试，分别验证 state/action 及应拒绝的负样本。
- 转换工作台：`tests/test_conversion_workbench.py`；覆盖数据集选择、instruction、批量转换、Doctor 和结果路径。
- 存储恢复：`tests/test_file_store.py`；损坏状态明确报错，中断提交可恢复。
- 输出：`tests/test_viewer.py` 覆盖无登录直连、Range 和过期行为，并实际检查每路视频。

集成测试自动查找 `.runtime/letools-venv/bin/letools`，也可设置 `PIPELINE_TEST_LETOOLS_BIN`。未安装工具时跳过不等于工具链通过。

AgileX 特定事实见 `docs/robots/agilex-aloha.md`；架构、存储与部署说明见 `docs/architecture.md` 和 `docs/deployment.md`。

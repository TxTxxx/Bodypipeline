# AgileX ALOHA 数据契约

本文记录当前 AgileX ALOHA 主从臂遥操作采集的 ROS2 接口、原始数据布局、state / action 语义、时间戳和 Profile 约束。内容基于对 AgileX ALOHA 现有 recorder、Piper 驱动、SDK、相机参数和上传样本的只读核查。

> 当前运行时只支持主从臂遥操作。Profile 中的历史限位字段仅作硬件说明，不参与转换准入。

## 1. 文件与版本

| 项目 | 路径 |
| --- | --- |
| 未审批通用模板 | `configs/agilex-aloha.json` |
| AgileX ALOHA 已核实 Profile | `configs/agilex-aloha-v2.json` |
| AgileX ALOHA Client 配置 | `configs/client.agilex-aloha.json` |

AgileX ALOHA 已核实 Profile：

```text
id: agilex_aloha_command_echo_v2
robot_type: agilex_aloha
control_mode: teleoperation
output_fps: 30
max_camera_skew_ms: 20
max_action_age_ms: 50
minimum_frames: 30
```

`agilex-aloha.json` 刻意保持 `semantics_approved=false`、`clock_verified=false`，不能用于真实数据转换。现场必须使用经过核实的 Profile 或在兼容性审查后列入 `accepted_capture_profile_hashes`。

## 2. ROS2 采集接口

### 2.1 CaptureService

```text
service: /data_tools_dataCapture/capture_service
type: data_msgs/srv/CaptureService
```

请求与响应：

```text
bool start
bool end
int32 episode_index
string dataset_dir
string instructions
string task_name
string task_descriptions
string task_id
---
bool success
string message
```

Bridge 的调用语义：

| 操作 | 请求 |
| --- | --- |
| 开始 | `start=true`, `end=false`, `episode_index=0`, `dataset_dir=<受管路径>` |
| 停止 | `start=false`, `end=true`, `episode_index=-1` |

不使用 `start=true,end=true`。Legacy recorder 将其实现为 toggle，网络重试可能反转用户意图。

Bridge 开始前会：

- 校验路径位于 `local_roots`；
- 确认没有其他 recorder worker；
- 确认所有相机和关节 topic 新鲜；
- 持久化 `STARTING` 意图后再调用 service；
- 等待唯一 recorder worker 和首条 `CaptureStatus`；
- 只有上述条件成立后才标记 `RECORDING`。

### 2.2 CaptureStatus

```text
topic: /data_tools_dataCapture/status
type: data_msgs/msg/CaptureStatus
```

字段：

```text
string[] topics
int32[] count_in_seconds
float32[] frequencies
bool fail
bool quit
```

限制：

- 没有 `session_id`、请求 ID、文件路径或总帧数；
- 不能仅凭任意 `quit=true` 证明当前 episode 已安全封存；
- Bridge 记录本机接收时间和 owner worker；
- `frequencies` 在启动初期可能无效，前端显示 unknown；
- `count_in_seconds` 不是总帧数。

## 3. 相机与关节流

### 3.1 相机

| ID | ROS2 topic | 输出 |
| --- | --- | --- |
| `left` | `/camera_l/color/image_raw` | `observation.images.left` |
| `front` | `/camera_f/color/image_raw` | `observation.images.front` |
| `right` | `/camera_r/color/image_raw` | `observation.images.right` |

Bridge 使用 `sensor_msgs/Image`，转换为 BGR8 后生成低延迟 JPEG 预览。录制原始图像由机器人 recorder 负责，预览不会修改原始画面。

### 3.2 关节与 action

| 输出 | 原始流 | 顺序 |
| --- | --- | --- |
| `action[0:7]` | `masterLeft.position` | 左臂 6 关节 + 夹爪 |
| `action[7:14]` | `masterRight.position` | 右臂 6 关节 + 夹爪 |
| `observation.state[0:7]` | `puppetLeft.position` | 左臂 6 关节 + 夹爪 |
| `observation.state[7:14]` | `puppetRight.position` | 右臂 6 关节 + 夹爪 |

ROS2 topic：

```text
/master/joint_left
/master/joint_right
/puppet/joint_left
/puppet/joint_right
```

每路 `sensor_msgs/JointState.position` 固定 7 维：

```text
joint0 ... joint5, joint6
```

- 前 6 维：弧度；
- 第 7 维：完整夹爪开度，单位米；
- 输出关节名使用 `left_joint_1..6, left_gripper, right_joint_1..6, right_gripper`。

### 3.3 action 语义

`/master/joint_*` 是 Piper 驱动从 CAN 控制消息读取的控制目标回显，不是独立测量的主臂姿态。

因此：

- action 表示绝对关节目标与夹爪开度；
- 不能把 action 当作“实际主臂关节反馈”；
- ROS 发布时间不能证明底层 CAN 指令的新鲜度；
- 静态回归通过不代表真实遥操作的动作语义已通过。

### 3.4 姿态流

```text
/puppet/end_pose_left
/puppet/end_pose_right
```

Bridge 订阅这两路用于新鲜度诊断，当前不作为正式 LeRobot 特征导出。

## 4. HDF5 采集包

Recorder 仍以时间戳文件写入临时目录，但 Client 的“保留并上传”会把它转换成单个
`episode.hdf5`。上传、服务端接收、转换和后续追溯都以该 HDF5 为单位；Server 不会把
它解包成 `episode0/` 文件树。

Recorder 暂存目录：

```text
<capture_raw>/episode0/
├── camera/color/
│   ├── left/*.jpg
│   ├── front/*.jpg
│   └── right/*.jpg
└── arm/jointState/
    ├── puppetLeft/*.json
    ├── puppetRight/*.json
    ├── masterLeft/*.json
    └── masterRight/*.json
```

文件名是 Unix 秒时间戳，例如：

```text
1760699999.123456.jpg
1760699999.123456.json
```

Client 打包后的 v1 HDF5 schema：

```text
/metadata
/streams/camera/<camera_id>/timestamps    float64[N]
/streams/camera/<camera_id>/jpeg          vlen uint8[N]
/streams/arm/jointState/<stream>/timestamps  float64[N]
/streams/arm/jointState/<stream>/position    float32[N, 7]
```

根属性包含 schema、`raw_format=agilex_joint_camera_hdf5_v1`、robot type 和 Profile ID；
`/metadata` 保存 session、instruction、采集时间和 Profile 摘要。相机 JPEG 保持原始编码，
关节数值保留 7 维顺序。Client 封存时会校验：

- 三路相机与四个主从臂流都存在；
- 文件不是符号链接，图像可解码且同一相机分辨率一致；
- 关节 `position` 为 7 个有限数值；
- 不允许空采集；
- HDF5 写完后 fsync，Manifest 只登记 `episode.hdf5` 的文件大小和 SHA-256。

Server 的 `AgileXProvider` 直接从 HDF5 流执行公共时间区间、30 Hz 网格、相机最近帧和
关节因果保持，输出 `work/<job_id>/aligned/episodeN.hdf5`。随后生成
`letools-hdf5-preset.json`，用 `letools convert --source-format hdf5` 直接转换，不再需要
任何解包步骤。旧版 `agilex_timestamped_directory_v1` 仍由同一 Provider 兼容读取。

一次批量任务会把同一 collection 的多个 `episode.hdf5` 先分别对齐为 `episode0.hdf5`、
`episode1.hdf5` 等，再转换成一个 LeRobot 数据集。批量要求同一 Runtime、Profile、collection、
采集模式和存储目标；任一 episode 失败时任务不会产出正式可预览数据集，但会保留每个 episode 的
诊断。批量上限为 1000 个 episodes。

本地保留策略与上传回执绑定：Server 持久化 ACK 且 Client 校验回执成功后，只删除 recorder 的
`raw/episode0/` 临时源目录；`episode.hdf5` 和 `manifest.json` 保留用于重传与追溯。ACK 前的任何
上传、提交或校验失败都不会触发清理。

## 5. 时间戳与对齐

### 5.1 时间域

| 数据 | 时间戳来源 |
| --- | --- |
| 相机 | Orbbec device timestamp，启用 host time sync |
| state | Piper SDK SocketCAN 时间戳 / 驱动反馈时间 |
| action | AgileX ALOHA ROS 发布时间，`use_sim_time=false` |
| 文件名 | ROS message `header.stamp` 的 Unix 秒 |

`clock_verified=true` 表示单位、来源和主机时间关系已经核查，不代表硬件级同步认证，也不证明 CAN 控制目标的新鲜度。

### 5.2 对齐规则

| 项目 | 值 |
| --- | --- |
| 输出帧率 | 30 Hz |
| 公共时间区间 | 所有必要流交集 |
| 相机匹配 | 最近帧 |
| 相机最大偏差 | 20 ms |
| state / action 匹配 | 因果前置样本 |
| 关节最大保持年龄 | 50 ms |
| 最短帧数 | 30 |

对齐失败示例：

```text
时间对齐越界：arm/jointState/puppetLeft frame=5 error=70.115ms limit=50.000ms
```

这表示输出网格第 5 帧缺少足够新的因果关节样本。首次现场出现该问题时，根因是 Legacy recorder 在订阅发现阶段就开始写 episode。修复和回归见[AgileX ALOHA recorder 启动时间空洞修复](agilex-recorder-fix.md)。

## 6. 限位与标定

Profile 保留了 Piper SDK 默认标称范围作为硬件说明元数据，但当前数据管线：

- 不检查 `state_limits`；
- 不检查 `action_limits`；
- 不执行 URDF 正逆运动学；
- 不裁剪超范围数值；
- 不因超范围拒绝转换。

限位、关节零位、夹爪标定和运动安全由机器人控制器、规划器和安全层负责。若采集值超出标称范围，应作为硬件标定或数据语义问题单独排查，不能通过修改转换器软件限位来掩盖。

## 7. Profile 审批

真实数据转换要求 Server Profile：

- `semantics_approved=true`；
- `clock_verified=true`；
- 14 个唯一 `joint_names`；
- 14 个逐维 `joint_units`；
- 与采集 Manifest 的 `robot_type` 和 Profile 摘要兼容。

Profile 摘要不匹配时，Server 会显示阻断原因。可以把已经独立核实的历史采集摘要加入 `accepted_capture_profile_hashes`，但不能修改原始 Manifest。

## 8. 验收边界

| 验收项 | 说明 |
| --- | --- |
| 代码与 API 测试 | 证明状态机、路径、上传和任务契约 |
| 静态采集 | 证明 recorder、Bridge、Client 和上传链路可启动停止 |
| 时间间隔回归 | 证明关节 / 相机没有启动空洞 |
| 真实 letools 转换 | 证明原始布局可被当前 Provider 消费 |
| Doctor / Visualizer | 证明输出结构和媒体可读 |
| 真实遥操作语义 | 仍需按具体任务验证 action-state 对应、夹爪标定与训练可用性 |

前三项或前五项通过，不能自动推出最后一项。

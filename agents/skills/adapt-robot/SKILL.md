---
name: adapt-robot
description: "为 BodyPipeline 新增机器人型号或操作方式（主从臂、VR、手柄、动捕等）时，核查采集接口、state/action 语义、原始格式与时钟，完成 Client、Profile、Server Provider、转换、QC 与预览适配；也用于修复现有机器人适配缺陷。不用于一般 UI 功能、模型训练或纯运动控制开发。"
---

# 机器人与操作方式适配

目标是让目标硬件及其操作方式走通：

```text
预览 → 启动采集 → 停止并确认排空 → 保留/丢弃 → 上传
→ Server 接收 → 对齐 → letools 转换 → QC/Doctor → Visualizer
```

本技能负责采集与数据处理适配；开发运动控制器、VR 交互应用、策略训练或通用 UI 仅在用户明确要求时纳入范围。

## 先建立当前基线

1. 阅读仓库根目录 `README.md`；存在 `AGENTS.md` 时一并遵守。
2. 检查 `git status`，保留用户已有修改，不覆盖无关工作。
3. 从当前代码确认，不凭旧文档猜测：
   - `mode` / `capture_mode` 允许值及 Adapter 选择；
   - `CaptureAdapter` 方法契约；
   - Profile 校验和转换准入；
   - Manifest、`raw_format` 与目录校验；
   - Server Provider、对齐、`--source-format` 和 QC 的实际分支。
4. 将任务归入下列一类或多类：

| 场景 | 典型改动 |
|---|---|
| 同型号新设备，接口/格式/语义不变 | 设备配置、独立密钥、数据根、Server 注册项 |
| 新采集接口，原始格式兼容 | 新 Adapter，或修改 ROS2 Bridge |
| 新原始格式或目录布局 | Robot packager、单文件 HDF5（推荐）或版本化 raw format、Manifest、接收、Provider 对齐、转换 |
| 新相机数、关节维度、动作表示 | Provider、特征 schema、对齐、转换检查、测试 |
| 同一设备的新操作方式 | Profile、action 来源、事件、时间语义和血缘 |
| 一个 Server 接入多种型号/操作方式 | 受信任的设备 → Profile/格式/Provider 路由 |

完整文件矩阵见[新机器人适配指南](../../../docs/robots/new-robot-adaptation.md)。该文档是 BodyPipeline 仓库资源；独立安装本技能时，以目标仓库实际代码为准。

## 必须保留的约束

- 使用文件目录与 JSON 持久化，不引入数据库；Python 依赖使用 uv 和对应锁文件。
- 机器人差异集中在 Adapter、Bridge、Profile、Provider 和 QC，不在页面、会话或上传主流程散布型号判断。
- 不默认 ROS2、主从臂或“主臂就是 action”。按实际接口记录操作输入、控制命令和机器人观测的边界。
- 未核实的 action 表示、维度顺序、单位、坐标系、时间域和事件语义保持未批准；不得猜测或填零绕过门禁。
- `start` 必须幂等；结果未知时不盲目重发。只有确认 writer 停止或排空后才能封存。
- 停止录制、暂停遥操作、控制器停机和急停是不同操作；采集适配不得隐式发送运动命令。
- 不通过放宽既有对齐阈值、批准标记或 QC 来让坏样本通过；原始采集和上传包保持不变。
- 优先把一次采集封存为单个自包含 HDF5，避免上传大量逐帧小文件；Server 应直接从 HDF5 对齐和生成 Letools mapping，不要求解包出原始目录。
- 新 HDF5 schema 必须使用独立、版本化的 `raw_format` 和根属性；字段相同但动作、时间或流语义不同也必须版本化，不能复用 AgileX 名称。
- Server 持久化 ACK 且回执校验通过后，Client 才可清理已封存到 HDF5 的 recorder 临时目录 `raw/episode0/`；`episode.hdf5` 与 Manifest 保留，上传或提交失败不得清理。
- 批量转换必须保留逐 episode 诊断并做全量校验；任一 episode 失败就不得生成正式可预览数据集，不能静默跳过或部分成功。
- 不在数据转换流程执行机器人软限位、关节位置范围或 URDF 运动学门禁。限位属于机器人控制器与安全层；Profile 中的 `state_limits` / `action_limits` 即使存在也只是可选说明，不是转换门槛。
- 真实数据不能标成 mock；新增真实后端必须继续经过语义审批和转换准入。Doctor 只生成质量报告，不承担发布或数据迁移。
- 读取文档或获得仓库写权限不等于获准操作真机。真机试录、运动命令和外部部署只按任务明确授权执行。

## 建立最小证据集

优先使用用户给出的接口文档、控制链路源码和脱敏样本。记录“已证实 / 未核实”及证据位置：

- 采集 start/stop 请求、响应、幂等性、owner、超时和写入完成依据；
- 操作输入 → 映射/重定向/IK/滤波/限幅 → 控制器命令 → 机器人观测；
- state、action、辅助输入和事件各自的来源、表示、维度、顺序、单位与时间戳；
- 相机/传感器 topic 或文件、消息类型、编码、QoS、采样率和时钟关系；
- episode 边界、控制权切换、跟踪失效、暂停/重置及 raw_format；
- 系统、Python ABI、ROS/SDK 包、数据根、Server 目标与现有测试。

缺少证据时继续做不依赖未知事实的代码、fixture 和离线验证；只询问会阻塞当前设计的信息。未知语义不得写成“通过”。

## 实施顺序

### 1. Client 与采集接口

复用 `CaptureAdapter` 和 Web API。ROS2 场景优先保留 Client → 本机 Bridge HTTP 契约；非 ROS 场景实现真实 Adapter，不引入无用 ROS 依赖。新后端必须接入部署方控制的显式选择机制，不能由前端或 Manifest 动态导入任意代码。

遵守[实现定位与适配契约](references/implementation-map.md)中的接口语义。预览失败与录制失败分开处理；过期帧不得冒充实时画面。

### 2. 原始数据与封存

把新格式贯通到：

```text
robot packager → episode.hdf5（或版本化替代格式）→ Manifest
→ raw_format → Server 接收校验 → collection 分组 → 任务血缘
```

对时间序列采集，优先定义可映射的 HDF5 schema：相机 JPEG、关节 position、各自 timestamp 和显式 metadata。Server 端应直接读取 HDF5 并生成 `aligned/episodeN.hdf5` 与 Letools HDF5 mapping preset；不得把解包原始目录作为架构前提。不兼容格式使用新的版本化 `raw_format`；格式相同但语义不同则版本化 Profile 和动作 schema。不要通过改名、补零、复制相机或截断关节迎合 AgileX 布局。

### 3. Server Provider、转换与 QC

新增 Provider 时：

- 输入契约和输出视图独立、可测试；
- 不修改上传的原始 HDF5；
- 对齐输出可继续使用单文件 HDF5，并通过 `--source-format hdf5 --preset ...` 交给 letools；
- 对齐报告记录流级误差、复用帧和缺失；
- 在 `pipeline/server/providers/` 新增或扩展 Provider，并注册到静态注册表；由认证设备绑定的 Runtime 路由，不让上传者选择任意插件；
- 真实/模拟来源、Profile、映射版本和标定版本进入去重逻辑与血缘；
- 特征名称、维度、单位和 Doctor 覆盖按实际 action/state 定义；不得重新引入关节/动作软限位门禁。
- 数据转换的 QC 只检查结构、有限数值、维度、时间、相机/视频完整性、动作语义与 Doctor 覆盖；不因数值超出机器人标称关节范围而拒绝、裁剪或丢弃 episode。

不要将主臂关节的因果保持策略直接套给位姿增量、速度、离散按钮或控制权事件。条件、坐标和动作表示的核查见[操作方式与动作语义](references/control-semantics.md)。

### 4. 预览与可消费性

确认转换结果的 episode、相机和每个 feature 能被 Visualizer 读取。结构验证或页面启动成功不等于视频可播放；至少逐路检查实际输出。

## 验证与交付

先用 Fake Adapter、脱敏样本或小型 fixture 验证，再按授权进行真机验收：

| 范围 | 最低证据 |
|---|---|
| 采集协议 | 重复 start、超时未知、owner、停止排空、失败录制不可封存 |
| 预览/封存/上传 | 每路画面、缺流、丢弃、摘要、续传、重启恢复 |
| 语义 | state/action 来源、顺序、单位、坐标、事件及应拒绝的负样本；不把软限位当转换准入 |
| 时间 | 时间戳单位/时钟域、对齐边界、缺失和重复采样语义 |
| Server | 正确 Runtime/Provider/Profile、未知格式阻断、批量禁止混 Runtime、11 项非运动学 Doctor、逐路预览 |
| 批量与恢复 | 1–1000 个 episodes 的顺序和计数、任一 episode 失败即整体失败、逐 episode 诊断、ACK 前不清理、重启后状态可解释 |
| 回归 | 原 AgileX Mock/真实样本与新样本共存，不互相误路由 |

常用检查：

```bash
uv sync --locked
uv lock --check
uv run --locked ruff check pipeline scripts tests
uv run --locked ruff format --check pipeline scripts tests
uv run --locked pytest -q
node --check pipeline/web/app.js
git diff --check
```

缺工具而跳过集成测试时标记未验证。`scripts/smoke.py` 只覆盖当前 Mock 布局，不能证明新机器人、VR 或真机链路通过。

交付代码、配置、锁文件、测试和接入记录，并分别说明：

- 离线适配是否完成；
- 真机试录与停止排空是否通过；
- 真实 letools / Doctor / Visualizer 是否通过；
- 数据是否完成了转换和 Doctor 质量报告；
- 仍未核实的语义、授权和现场风险。

## 技能资源

- [操作方式与动作语义](references/control-semantics.md)：新控制方式、位姿/增量动作、事件或时间模型。
- [实现定位与适配契约](references/implementation-map.md)：当前文件边界、Adapter 契约、已知硬编码和“禁止软限位门禁”的实现约束。
- [机器人接入记录模板](assets/robot-onboarding.template.md)：复制为 `docs/robots/<robot-key>-<operation-key>.md` 并填写证据。

适配完成并准备部署到现网时，继续使用 [`deploy-robot-adapter`](../deploy-robot-adapter/SKILL.md)。适配技能解决“数据链能否工作”，部署技能解决“新机器人的 Server 改动会不会影响其他机器人”；两者不能互相替代。

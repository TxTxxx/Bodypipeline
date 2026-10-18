# 代码导读

本文按一次数据的实际流动顺序介绍模块职责。架构图和状态图见[架构设计](architecture.md)。

## 1. 入口与进程角色

```bash
uv run --locked python -m pipeline.cli <role> --config <config>
```

| role | 默认端口 | 作用 |
| --- | ---: | --- |
| `client` | 8080 | Client Web、采集状态机、上传队列 |
| `server` | 8081 | 接收、数据集、转换、质检、Viewer 调度 |
| `bridge` | 8090 | 机器人本机 ROS2 桥，仅 loopback |
| `viewer` | 8082 | Visualizer UI / 数据 / 视频代理 |

`pipeline/cli.py` 延迟导入各角色入口，避免 Mock 模式被迫加载 ROS 或 letools。所有服务固定 `workers=1`，Bridge 强制 loopback。

## 2. 共享层

```text
pipeline/shared/
├── config.py
├── core.py
├── hdf5.py
├── runtime.py
├── security.py
├── collections.py
└── web.py
```

| 文件 | 职责 |
| --- | --- |
| `config.py` | `Settings` 加载、环境变量覆盖、密钥长度与互异检查、Profile 读取 |
| `core.py` | `DomainError`、规范化摘要、路径约束、原子 JSON、`Store`、`ProcessLock` |
| `hdf5.py` | AgileX 原始/对齐 HDF5 schema、Client 单文件打包、元数据读取和 HDF5 定位 |
| `runtime.py` | `RobotRuntime` 不可变路由记录：Runtime、Provider、Profile 和允许的 `raw_formats` |
| `security.py` | 登录、Cookie、设备 / Bridge 鉴权、Origin 校验、安全响应头 |
| `collections.py` | 数据集名称规范化、collection ID 计算、上传分组 |
| `web.py` | 挂载两侧共用的原生 Web 页面 |

### 2.1 文件 Store

`Store` 的存储结构：

```text
root/state/<kind>/<id>.json
```

没有数据库、索引进程或后台 broker。列表操作扫描对应类型目录，按 `created_at` 倒序返回。写入采用临时文件、`fsync`、原子替换和父目录 `fsync`。

同一数据根由 `ProcessLock` 使用 `flock` 限制为单写进程。跨文件操作依赖意图状态和启动恢复扫描，而不是数据库事务。

### 2.2 路径与摘要

- `safe_relative()` 只接受受控相对路径。
- `confined()` 防止目录穿越，并逐段检查符号链接。
- `digest()` 使用规范 JSON 生成稳定摘要。
- `file_hash()` 用于逐文件 SHA-256。
- `atomic_json()` 保证状态和 Manifest 不会留下半写文件。

## 3. Client

```text
pipeline/client/
├── app.py
├── service.py
├── upload.py
├── bridge.py
└── adapters/
    ├── base.py
    ├── mock.py
    └── ros2.py
```

### 3.1 `app.py`

创建 FastAPI 应用并启动后台协程：

```text
UploadAgent.process_once   上传队列
CaptureService.health      录制健康与磁盘低水位
CaptureService.seal_pending 补做中断的封存
```

主要 API：

```text
/api/config
/api/health
/api/targets
/api/captures
/api/captures/start
/api/captures/{id}/start|stop|keep|discard
/api/captures/{id}/retry-upload
/api/cameras/{id}/frame
```

页面轮询 `capture-status`，不把完整 Profile、Manifest 和上传回执塞进每次刷新。

### 3.2 `service.py`

`CaptureService` 是 Client 的核心状态机：

- 创建 collection 目录和 capture ID；
- 通过 Adapter 启动 / 停止 recorder；
- 检查本地根、目录、磁盘水位和机器人健康；
- 停止后进入 `REVIEW`，等待人工保留或丢弃；
- 保留时把 recorder 的时间戳流打包为 `episode.hdf5`，校验、`fsync`、计算 SHA-256 并写 Manifest；
- 封存成功后加入单文件上传队列；
- Client 重启时把未知的录制状态转为 `RECOVERY_REQUIRED`。

目录结构：

```text
<root>/datasets/<collection_id>/sessions/<capture_id>/
├── manifest.json
├── episode.hdf5
└── raw/episode0/              # 上传 ACK 后清理
```

### 3.3 `upload.py`

`UploadAgent` 执行：

1. 读取并重新校验本地 Manifest；
2. 向 Server 创建上传记录；
3. 从 Server 返回的 offsets 继续上传；
4. 每块发送 `X-Chunk-SHA256`；
5. 校验 Server 提交回执；
6. 成功后保存 `ACKED`，失败按退避重试。

上传是顺序的，支持字节级断点续传。新 AgileX ALOHA 采集只有一个 `episode.hdf5`，因此显著减少了小文件 RPC 与元数据开销；单个损坏块会触发该文件 reset，不会静默跳过。Server ACK 后 Client 才清理本地 recorder 暂存目录。

### 3.4 `adapters/base.py`

统一采集契约：

```text
health()
start(session_id, raw_root, instruction)
stop(session_id)
status()
frame(camera_id)
close()
```

适配器的关键语义：

- `start` 必须幂等，并确认 writer 已开始；
- `stop` 成功必须表示写入结束或已排空；
- 状态未知时不能盲目重试；
- `frame` 只返回新鲜 JPEG，预览失败不能影响录制；
- `close` 不应隐式启动或停止机器人。

### 3.5 `bridge.py` 与 `adapters/ros2.py`

`Ros2Adapter` 只通过本机 HTTP 与 Bridge 通信。Bridge：

- 使用 `rclpy` 调用机器人 ROS2 采集 service；
- 订阅状态 topic 和相机 topic；
- 等待 recorder 首条状态后才把采集标记为就绪；
- 把图像转换为 JPEG 预览；
- 记录 topic 新鲜度并暴露健康状态。

这是代码中唯一允许依赖 ROS2 Python 的边界。

## 4. Server

```text
pipeline/server/
├── app.py
├── ingest.py
├── imports.py
├── storage.py
├── collections.py (shared)
├── jobs.py
├── providers/
│   ├── base.py
│   ├── registry.py
│   └── agilex.py
├── alignment.py
├── datasets.py
├── checks.py
├── quality.py
└── viewer.py
```

| 文件 | 职责 |
| --- | --- |
| `app.py` | FastAPI 路由、后台循环、角色组装 |
| `ingest.py` | 上传建单、分块校验、续传、提交、Manifest 读取 |
| `imports.py` | 历史原始采集 / LeRobot 原地登记 |
| `storage.py` | `data_root/<robot_type>/{incoming,raw,work}` 路径与目录准备 |
| `collections.py` | collection 计算和上传分组 |
| `jobs.py` | 通用任务编排：Runtime 路由、幂等、Provider 调用、letools 子进程和任务状态 |
| `providers/registry.py` | 静态 Provider 注册表与 `RuntimeCatalog`；设备不能动态选择代码 |
| `providers/agilex.py` | AgileX Provider：Profile 校验、对齐、质量、转换参数和 feature 修补 |
| `shared/runtime.py` | `RobotRuntime` 不可变路由记录 |
| `alignment.py` | AgileX 三相机、四路关节的 30 Hz 有界对齐 |
| `datasets.py` | 从任务记录解析可预览数据集和格式 |
| `checks.py` | 独立质检任务、deep validate / Doctor 编排 |
| `quality.py` | Profile 阻断项、11 项 Doctor schema 与安全策略 |
| `viewer.py` | 单实例 Visualizer 生命周期和只读网关 |

### 4.1 上传接收

`IngestService` 负责：

- 创建设备上传记录；
- 校验 `robot_id` / `project_id`；
- 接收 offset + chunk；
- 校验块摘要；
- 支持文件 reset；
- 完整文件 SHA-256 与 Manifest 校验；
- 原子提交到 `raw/`。

Server 不直接信任 Manifest 中的文件名、大小或路径；所有路径都经过 `safe_relative` / `confined`。

### 4.2 数据集分组

`collection_metadata()` 使用以下 scope 计算 collection ID：

```text
dataset_name
project_id
robot_type
profile_hash
capture_mode
raw_format
target
prefix
```

这样同名但不同机器人、Profile、格式或目标的采集不会混在同一数据集。Page 的机器人筛选只是展示层，不能替代 collection 计算或设备鉴权。

### 4.3 `jobs.py`

`JobRunner` 是转换编排器：

1. 校验 upload 状态、collection、instruction 和目标格式；
2. 根据上传记录绑定（或旧记录的 Manifest 兼容规则）解析同一 Runtime；
3. 拒绝混合 Runtime 的批量任务，按 Runtime/Profile/Provider 生成幂等键；
4. 逐 episode 让 Provider 执行 Profile 校验、对齐和质量策略；
5. 由 Provider 生成固定版本 letools `convert` 参数；
6. Provider 修补目标数据集 feature metadata，Server 运行结构检查和 deep validate；
7. 可选运行 Doctor；
8. 保存 `work/<job_id>/` 报告、Runtime/Provider/Profile 血缘和任务状态。

批量任务先逐 episode 完成快照校验和对齐，全部通过后才调用一次 letools。每个 episode 的失败原因
写入 `episode_diagnostics`，但任一 episode 失败都会阻止正式数据集生成，不会静默跳过。Server
重启时 `RUNNING` 任务转为 `INTERRUPTED`，不会自动重放外部工具。

### 4.4 `alignment.py`

对齐器包含两个入口：

- `prepare_agilex_hdf5()`：当前主路径。直接读取上传包中的时间戳相机 / 关节流，流式生成
  Letools-native 的单文件 `aligned/episodeN.hdf5`，不解包原始目录。
- `prepare_agilex()`：旧版时间戳目录兼容路径，仅用于 `agilex_timestamped_directory_v1`。

旧目录兼容输入仍是：

```text
camera/color/{left,front,right}/*.jpg
arm/jointState/{puppetLeft,puppetRight,masterLeft,masterRight}/*.json
```

行为：

- 计算所有必要流的公共时间区间；
- 按 30 Hz 建立统一网格；
- 相机选最近帧，最大 20 ms；
- 关节只取因果前置帧，最大保持 50 ms；
- 新 HDF5 路径不创建 `episode0/` staging，也不复制或修改上传包；
- 旧目录路径使用 staging symlink 保持原始文件不变；
- 每个 episode 生成对齐报告。

新路径输出 `observation.state`、`action`、三路 `observation.images.<camera>`，并由 Provider 生成
`letools-hdf5-preset.json` 供 `letools convert --source-format hdf5` 使用。对齐器只检查维度、
有限数值、媒体可解码性和时间边界，不检查关节限位、URDF 或运动学。

### 4.5 `checks.py` 与 `quality.py`

`QualityRunner` 对单个已完成任务执行：

```text
letools validate
letools doctor check --json
```

Doctor 固定执行 11 项非运动学检查。通用 `quality.py` 还负责 Profile 阻断项和 names/units 一致性；具体维度由 Provider 声明。当前 `AgileXProvider` 要求：

- `semantics_approved`;
- `clock_verified`;
- 14 个唯一 `joint_names`；
- 14 个逐维 `joint_units`。

即使 Profile 中存在 `state_limits` / `action_limits`，当前代码不会把它们用于转换或 Doctor 门禁。

### 4.6 `imports.py`

`ImportService` 是管理端历史数据入口：

- 绝对路径；
- 白名单根；
- 拒绝符号链接；
- 识别原始 Manifest 或 LeRobot metadata；
- 原始数据登记为 upload/collection；
- LeRobot 登记为可直接预览和质检的数据集；
- 原地登记、内容摘要幂等、不复制和改写。

### 4.7 `viewer.py`

`ViewerManager` 负责：

- 启动 / 停止 Visualizer 子进程；
- 等待 UI 和数据桥就绪；
- 代理 UI、数据集请求和视频 Range；
- 记录活动会话和 TTL；
- Server 页面先请求 `/api/jobs/{id}/viewer`，再打开 `viewer_origin`。

Viewer Gateway 不安装登录鉴权。无活动会话时返回 503，不会重定向到登录页。

## 5. Web 页面

```text
pipeline/web/
├── index.html
├── app.js
└── style.css
```

页面由 Client / Server 共用，根据 `/api/config` 的 `role` 切换布局：

- Client：相机预览、数据集、存储根、远端目标、采集按钮和上传状态；
- Server：原始数据、数据转化、预览与质检三个侧边栏工作区。

页面不需要接口文档，也不保存管理密钥到 localStorage。请求通过 HttpOnly Cookie 进行管理鉴权。

## 6. 测试入口

```bash
uv run --locked pytest -q
uv run --locked ruff check pipeline scripts tests
uv run --locked ruff format --check pipeline scripts tests
node --check pipeline/web/app.js
```

测试重点：

| 领域 | 覆盖 |
| --- | --- |
| Client | 启停幂等、健康、预览、封存、丢弃和恢复 |
| 上传 | 分块、续传、摘要、损坏块和回执 |
| 数据集 | collection 分组、机器人类型、历史和导入 |
| 转换 | 对齐阈值、批量 episode、格式、幂等 |
| 质检 | 结构检查、Doctor schema、WARN / FAIL |
| Viewer | 会话生命周期、无鉴权入口、Range |
| 存储 | 路径约束、原子状态、迁移 |

真实 letools 集成测试需要独立工具环境；未安装时相关测试会跳过，不能把跳过当成通过。

## 7. 修改代码时的边界

- 机器人差异放在 Adapter / Bridge / Profile / Provider，不放在 Web 页面或 `jobs.py` 型号分支。
- 新原始格式必须同步 Manifest、Ingest、collection、Provider、Runtime `raw_formats` 和测试。
- 新增机器人类型必须注册 Runtime/Provider、绑定设备身份并验证设备隔离，不能只改目录名。
- 不得为通过数据加入关节软限位或 URDF 运动学门禁。
- 转换结果固定在 `work/<job_id>/dataset`，不要重新引入额外的数据集晋级或导出步骤。
- Viewer 入口保持无鉴权，但部署上必须限制网络访问范围。

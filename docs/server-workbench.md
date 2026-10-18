# Server 工作台

Server 页面用于管理原始数据、提交批量转换、运行质检和打开只读预览。页面采用侧边栏切换三个工作区：

1. **原始数据**
2. **数据转化**
3. **预览与质检**

所有结果都保存在 Server 文件系统，不使用数据库。页面中显示的路径都是运行 Server 的主机路径，不是操作者本机路径。

## 1. 原始数据

### 1.1 机器人类型优先

顶部机器人类型选择器同时筛选：

- 数据集；
- 已上传 episodes；
- 转换任务；
- 预览与质检结果；
- 页面统计数量。

操作顺序应为：

```text
选择机器人类型 → 选择数据集 → 选择已完成上传的 episodes
```

切换机器人类型会清空上一类型的数据选择，避免把不同 Profile 或原始格式误提交到同一任务。后台轮询刷新会保留当前选择。

无法从 Manifest 或 Profile 恢复机器人类型的历史记录会归入“未标注类型”，不会冒用当前服务器默认型号。

### 1.2 数据集与 episode

每次 Client 采集都会追加到对应 collection。Server 展示：

- 数据集名称；
- 机器人类型；
- episode 数量；
- 已完成上传 / 总 episode 数；
- 总大小；
- 存储目标和子目录。

只有上传状态为 `COMMITTED` 的 episode 可以勾选转换。未完成上传的记录仍显示在数据集中，便于排查断点续传或失败。

### 1.3 导入历史数据

点击“原始数据 → 导入历史数据”，填写 Server 上的绝对路径。

支持结构：

| 路径内容 | 行为 |
| --- | --- |
| `manifest.json` + `episode.hdf5` | 登记为当前 HDF5 原始采集 |
| `manifest.json` + `data/episode0` | 登记为旧原始采集 |
| `manifest.json` + `raw/episode0` | 登记为更早原始采集，兼容旧 Client |
| `meta/info.json` 且版本为 v2.1 / v3.0 | 登记为已有 LeRobot 数据集 |

导入要求：

- 路径在 `import_roots` 或 `data_root` 白名单内；
- 路径是普通目录，不是符号链接；
- 机器人类型可从 Manifest / LeRobot 元数据读取，或在页面显式指定；
- 显式类型与文件声明冲突时拒绝；
- 同一路径重复导入是幂等的；
- 源数据不被复制、移动或改写。

原始采集导入后可以进入批量转换；LeRobot 导入后可以直接预览和运行 Doctor。

## 2. 数据转化

### 2.1 创建转换任务

在原始数据中勾选同一数据集的 episodes 后，进入“数据转化”：

1. 确认数据集和 episode 列表。
2. 填写整个批次统一的 instruction。
3. 选择目标格式：
   - LeRobot `v3.0`（默认）；
   - LeRobot `v2.1`。
4. 选择是否在转换完成后自动运行 Doctor。
5. 提交任务。

要求：

- 至少选择一个 episode；
- 所有 episodes 必须属于同一 collection、机器人类型、Profile、采集模式和存储目标；
- instruction 长度为 1–2000 字符；
- 批量上限为 1000 个 episodes。

相同来源、Manifest 摘要、instruction、Profile、目标格式和工具版本会复用未失败任务。同一输入不会因为重复点击生成多个处理任务。

### 2.2 处理阶段

```text
排队
→ Profile / 所有权 / 格式校验
→ 逐 episode 从 HDF5 时间对齐
→ letools convert --source-format hdf5
→ 转换结构检查
→ deep validate
→ 可选 Doctor
→ 可预览结果
```

批量任务会继续核验后续 episodes，并把每个 episode 的结果写入 `episode_diagnostics`。一个 episode 失败不会掩盖其他 episodes 的具体错误，但整个任务不会生成可预览的正式数据集。

常见状态：

| 状态 | 含义 |
| --- | --- |
| `QUEUED` | 等待执行 |
| `RUNNING` | 当前处理中 |
| `BLOCKED` | Profile、语义、时钟或机器人类型不匹配 |
| `FAILED` | 对齐、转换或工具执行失败 |
| `DEMO_READY` | 演示模式完成，不包含真实 LeRobot 数据 |
| `CONVERTED` | 转换完成且结构验证通过，未运行 Doctor |
| `SYNTHETIC_READY` | 模拟数据通过真实工具链 |
| `COMPLETED` | Doctor 总体 PASS |
| `QUARANTINED` | Doctor 有 WARN / FAIL，数据保留供排查 |
| `INTERRUPTED` | Server 重启中断，需要显式重试 |

### 2.3 时间对齐

当前 AgileX 适配器要求：

- 三路相机：`left`、`front`、`right`；
- state：`puppetLeft`、`puppetRight`；
- action：`masterLeft`、`masterRight`；
- 每路关节 7 维；
- 30 Hz 输出；
- 相机最大偏差 20 ms；
- 关节因果保持最大 50 ms；
- 最短 30 帧。

出现如下错误时：

```text
时间对齐越界：arm/jointState/puppetLeft frame=5 error=70.115ms limit=50.000ms
```

说明指定 episode 的第 5 个输出帧之前没有足够新的关节样本。应检查 recorder 启动阶段、topic 连续性、时间戳单位和写入延迟，不应直接放宽阈值。

### 2.4 转换结果路径

转换结果固定保存在：

```text
<data_root>/<robot_type>/work/<job_id>/dataset
```

Server 当前 AgileX 数据示例：

```text
/srv/bodypipeline-data/agilex_aloha/work/<job_id>/dataset
```

任务工作目录同时包含：

```text
work/<job_id>/
├── aligned/episodeN.hdf5     # 对齐后的 Letools 输入
├── letools-hdf5-preset.json  # 字段映射
├── dataset/                  # 可预览 LeRobot 数据集
├── checks/<check_id>/        # 独立质检报告与日志
├── alignment-report.json
├── robot-report.json
├── lineage.json
└── convert / validate / doctor 日志
```

页面在“预览与质检 → 转换结果”显示可复制的完整数据集路径。只有达到可预览状态且 `dataset/meta/info.json` 存在时，结果才会标记为可用。

没有额外的数据集晋级目录。Doctor 通过、WARN 或 FAIL 都不会移动结果目录。

## 3. 预览与质检

### 3.1 运行 Doctor

对已有转换结果可以单独点击“运行质检”。Server 会依次执行：

1. 转换结构检查；
2. `letools validate`；
3. `letools doctor check --json`；
4. 保存报告和日志。

Doctor 固定执行 11 项非运动学检查：

```text
metadata
temporal
actions
videos
statistics
episodes
consistency
training
anomalies
portability
per_episode
```

明确不执行：

- 关节 / 夹爪软限位检查；
- URDF 正逆运动学检查；
- 自动修复、裁剪或删除。

Doctor 报告中如果出现 WARN / FAIL，数据集仍保留，仍可预览，方便定位问题。

### 3.2 打开 Visualizer

在“预览与质检”中选择可用任务，点击“打开数据预览”。

行为：

- Server 启动单实例只读 Visualizer；
- 页面等待会话进入 `READY`；
- 浏览器打开 Viewer 入口；
- Viewer 不需要登录；
- 会话默认 30 分钟过期；
- 同一时间只保留一个活动预览会话。

Viewer 入口无鉴权，只应通过内网、SSH 隧道或受保护的反向代理访问。无鉴权不代表适合直接暴露公网。

### 3.3 预览不可用

可能原因：

| 现象 | 检查项 |
| --- | --- |
| 没有“打开数据预览”按钮 | 任务尚未达到可预览状态 |
| Viewer 返回 503 | 未启动会话或会话已过期 |
| 页面空白 | Bun、Visualizer 进程、Viewer Gateway 或浏览器控制台 |
| 视频不能拖动 | 数据桥 / Range 转发异常 |
| 某些相机缺失 | LeRobot 输出视频特征或转换日志 |

## 4. 限位与质量边界

数据转换和 Doctor 不执行机器人软限位或 URDF 运动学门禁。Profile 中的 `state_limits` / `action_limits` 只作为硬件说明元数据保留，不是数据转换准入条件。

即使原始数值超出标称范围，转换链也不会：

- 裁剪值；
- 删除 episode；
- 拒绝转换；
- 自动修改夹爪单位。

机器人限位、关节标定、控制器安全包络和运动急停由机器人控制器与安全层负责。若数据数值异常，应作为数据语义或硬件标定问题单独排查，而不是在转换器中添加软件限位。

## 5. 历史任务兼容

升级前记录的任务可能包含旧的 `dataset_path` 或不同目录布局。Server 会在配置允许的根目录内只读解析这些路径：

- 不移动；
- 不复制；
- 不重写；
- 不重新生成。

路径不在 `root`、`data_root` 或 `import_roots` 内时不会开放预览。旧报告中的检查项数量和“运动学检查被跳过”记录保持原样，不与当前 11 项报告混为一谈。

## 6. 数据维护

- 原始数据位于 `data_root/<robot_type>/raw/`。
- 任务工作区位于 `data_root/<robot_type>/work/`。
- 导入数据保留在源目录，由 Server 记录引用。
- 转换失败时保留工作目录和对齐报告用于诊断。
- 清理、归档和备份必须由运维流程执行；页面不提供批量物理删除。
- 备份时必须同时保存状态 JSON、Manifest、原始数据、转换结果和报告。

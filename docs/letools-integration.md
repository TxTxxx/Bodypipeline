# letools 集成

BodyPipeline 不实现 LeRobot 转换和 Doctor 算法，而是在 Server 上通过子进程调用固定版本的 [XieWeikai/letools](https://github.com/XieWeikai/letools)。

## 1. 固定版本

| 项目 | 值 |
| --- | --- |
| 上游仓库 | `https://github.com/XieWeikai/letools` |
| 固定提交 | `1c417cc9c205f85cd01a5e5860646f1594738354` |
| Python | 3.12 |
| 工具环境 | `.runtime/letools-venv/` |
| letools 源码 | `.runtime/vendor/letools/` |
| Python 锁文件 | `environments/letools/uv.lock` |

安装：

```bash
bash scripts/install_letools.sh
```

安装脚本会检查源码 checkout 是否干净、切换到固定提交、初始化子模块，并使用独立 uv 项目同步依赖。Server 配置中的 `letools_bin` 应指向：

```text
.runtime/letools-venv/bin/letools
```

不要在 ROS Bridge 环境中安装 letools，也不要让 letools 依赖覆盖 Client / Server 主环境。

## 2. 上游能力边界

letools 是数据转换、验证、质检和可视化工具，不是机器人控制、上传、身份权限或任务编排平台。

BodyPipeline 负责：

- 机器人采集与上传；
- Manifest 和原始文件不可变封存；
- collection 分组；
- Server 身份与设备边界；
- 时间对齐 staging；
- 任务状态、日志、重试和报告展示；
- Viewer 会话生命周期。

letools 负责：

- 读取支持的输入格式；
- 转换为 LeRobot v2.1 / v3.0；
- deep validate；
- Doctor 检查；
- Visualizer 前端与数据桥。

当前 AgileX Provider 的主路径是单文件 HDF5：

```text
--source-format hdf5 --preset <generated-preset>
```

旧版 `agilex_timestamped_directory_v1` 仍使用 `--source-format agilex` 兼容读取，但不是新采集的默认
路径。其他 Provider 可以声明不同的 source-format 和输入布局；这不代表 letools 能直接读取所有
ROS2 bag、MCAP 或任意机器人目录。新增输入格式必须在 Server 侧增加 Provider / staging adapter，
或先确认上游已有可用 source provider。

## 3. Server 调用链

### 3.1 环境诊断

任务开始前和安装后执行：

```bash
<letools_bin> doctor environment
```

非零退出会阻断转换任务。

### 3.2 转换

`AgileXProvider` 从上传的 `episode.hdf5` 读出时间戳流，生成
`work/<job_id>/aligned/episodeN.hdf5`，再根据受信任 Profile 生成
`letools-hdf5-preset.json`。转换命令由 Provider 生成，不是通用 `JobRunner` 的硬编码：

```bash
<letools_bin> convert \
  <work>/aligned \
  <work>/dataset \
  --source-format hdf5 \
  --preset <work>/letools-hdf5-preset.json \
  --to v3.0 \
  --workers 1 \
  --video-workers 1 \
  --video-codec libx264 \
  --video-pixel-format yuv420p
```

HDF5 preset 明确声明：

- `action` 与 `observation.state` 的 source/target key、float32 dtype 和关节名；
- 三路 `observation.images.<camera>` 的 source key、宽高与 JPEG 编码；
- 输出 fps、robot type 和本次任务的 instruction。

因此 Server 不需要解包原始 HDF5，也不需要恢复时间戳 JPEG/JSON 目录。旧版
`agilex_timestamped_directory_v1` 仍可通过 `--source-format agilex` 兼容转换。

目标格式支持：

- `v3.0`
- `v2.1`

转换后 Server 会回填 Profile 中的 `joint_names` 到 `meta/info.json`，并检查 episode / frame 数量与输入快照一致。

### 3.3 深度结构验证

```bash
<letools_bin> validate <dataset> --deep
```

这一步验证目录、元数据、schema、Parquet 和媒体可读取性。失败会阻断预览和 Doctor。

### 3.4 Doctor

```bash
<letools_bin> doctor check <dataset> \
  --checks metadata,temporal,actions,videos,statistics,episodes,consistency,training,anomalies,portability,per_episode \
  --ci \
  --fail-on warn
```

BodyPipeline 固定排除 `kinematics`，只运行上述 11 项非运动学检查。原因：

- 关节限位和 URDF 运动学属于机器人控制器与安全层；
- 当前 LeRobot 元信息不足以可靠推断所有机器人的运动学约束；
- 数据管线不应裁剪、拒绝或改写超出 Profile 标称范围的原始值。

Doctor 的 JSON 报告必须与数据集 `meta/info.json` 中的 episode 数和帧数一致，并包含预期的 11 项检查。退出码和报告 severity 一起用于状态判定：

| 结果 | 任务状态 |
| --- | --- |
| PASS | `COMPLETED` 或 `SYNTHETIC_READY` |
| WARN / FAIL | `QUARANTINED` |
| 工具异常、报告无效、检查缺失 | `FAILED` |

WARN / FAIL 不会删除结果目录，仍可在 Server 中预览和查看报告。

#### Episode 质量评分

Visualizer 的 Episode 侧栏显示逐片段总分（0–100），选中片段时展示动作平滑度、动作覆盖度、
跨片段一致性和长度四个分项，以及原算法建议复查的提示。评分不参与 Doctor 质量门禁，也不执行
数据修复、裁剪或删除；分数是启发式指标，不代表任务成功率，也不适合作为不同机器人间的统一质量标尺。

首次打开预览后，浏览器通过 Viewer Gateway 的 `POST /api/episode-scores/<dataset_id>` 提交评分，
再用同路径 GET 读取状态与结果。接口沿用 Viewer 鉴权、同源校验和预览有效期；只接受已登记的数据集 ID。
评分运行于现有 LeTools Python 环境中的独立 CPU 子进程，不占用转换队列；Viewer 最多同时计算一个数据集，
播放可以继续。结果缓存于私有 `state/episode-score/`，原数据集保持只读。元信息或 Parquet 分片变化后，
旧结果失效并在再次打开/轮询时重新计算；计算期间发生变化则不发布该次结果。

固定 Doctor 的 `score` 数据加载器会展平向量动作、遗漏后续分片。`episode_score_worker.py` 在独立
子进程中替换该加载步骤，合并同一 Episode 的各分片并保持动作矩阵的 `(帧数, 维度)`，随后调用
原版 `score_episodes` 算法和权重；不修改上游源码。每个声明的 Episode 都保留一行，缺数据、
少于三帧、索引异常或含 NaN/Inf 时显示“无法评分”及原因。有效 Episode 不足四个时，原算法
一致性项未评估且按零计入总分；界面显示该项为 `—` 并说明这一限制。更新 Doctor 固定版本时需重新验证适配。

### 3.5 Visualizer

BodyPipeline 不再为每个数据集执行 `letools visualizer serve`（它把应用绑定到单个数据集，
导致同一时间只能预览一个）。Server 改为：

```bash
<letools_bin> visualizer setup --cache-dir <server_root>/viewer-cache   # 准备固定版本源码并应用补丁
bun run build   # DATASET_URL=<viewer_origin>/_data，仅在输入变化时构建
bun run start --hostname 127.0.0.1 --port <viewer_ui_port>
```

Visualizer 由 `pipeline/server/viewer_bootstrap.py` 启动并常驻；网关用
`<viewer_origin>/_data/bp/<job_id>/resolve/main/...` 这一与 Hugging Face 相同的 URL 形态
按数据集提供文件，并在 `/_data/doctor/?dataset=bp/<job_id>` 提供 Doctor 报告。

要求：

- Bun 1.3.9 位于 `viewer_bun` 或服务 `PATH`；
- UI 和数据桥只监听 loopback；
- Viewer Gateway 代理 UI、数据 API 和视频 Range；
- 只读、关闭 annotation 后端；
- 可同时登记多个数据集，各自独立；
- 预览链接默认 30 分钟过期；
- Viewer 入口与 Server 共用 `PIPELINE_TOKEN`，未登录返回登录页；仍应只监听内网或 Tailscale 地址。

关节曲线固定为每个通道一个图框，同一通道的 `action` 与 `observation.state` 放在一起对照。
不按数值范围组合不同通道，也不提供“合并显示”模式。启动时自动应用
`pipeline/server/viewer_channels.patch`；补丁内容参与构建指纹，更新后会在下次启动
Visualizer 时重新构建，无需重新转换数据集。

`viewer_presentation.patch` 和 `pipeline/server/viewer_ui/` 在上述基础上提供信号浏览器：
关节位置、关节速度、力矩、末端位姿、底盘位姿及底盘速度分别查看。每张图仅显示
同一通道、同一类别的信号，指令使用蓝色实线，实测使用琥珀色虚线；未知字段独立成类。
原始采样和单位不变，支持图例开关、时间联动、通道筛选和单列放大。三路视频等宽铺满，
保持画面比例，顺序为左腕、Head、右腕；支持隐藏、恢复、放大和 Esc 退出放大。

工作台、预览、登录和质检报告默认使用深夜模式，可切换浅色。`pipeline/web/theme.js`
通过同主机 Cookie 在 Server / Viewer 端口间共享偏好，并用 localStorage 保留设置。
页面头部先初始化主题，避免进入页面时闪白。所有展示补丁、UI 源文件与主题脚本都参与
构建指纹；部署后重启服务即可更新，结果文件无需移动或重新转换。

### 3.6 中文基础补丁与主题覆盖

上游 Visualizer 是英文深色界面。BodyPipeline 通过 letools 的集成补丁机制，在准备 Visualizer
源码时叠加一个中文化与主题补丁，不修改上游子模块：

```text
patches/letools-visualizer/0002-zh-cn-light-theme.patch
```

安装（在已执行过 `scripts/install_letools.sh` 的机器上）：

```bash
bash scripts/install_visualizer_zh_patch.sh
```

脚本会用 letools 自身的解析逻辑找到补丁目录，并把补丁复制进去，因此同时支持
`git` checkout 与打包安装两种 letools 布局。补丁集合会进入 Visualizer 源码指纹，
下一次打开预览时会自动重建缓存并重新 `bun install`。下表描述基础中文补丁；
启动时还会叠加上一节的信号浏览器与深夜主题，最终主题以用户偏好为准。

补丁内容：

| 范围 | 变化 |
| --- | --- |
| 文案 | 标签页、侧边栏、统计、动作分析、筛选、帧预览、标注、Doctor、错误与提示全部中文 |
| 主题 | 深色改为 BodyPipeline 浅色：`#f7f8fa` 背景、白色面板、`#2455eb` 主色、相同圆角与字体 |
| 调色板 | 通过 `@theme` 重映射 slate / cyan / 红绿黄，避免逐个组件改写 className |
| 图表 | Recharts 网格、坐标轴、Tooltip 改为浅色，原有数据配色保留 |
| 标注 | `annotations-skin.css` 表面与前景色改为浅色 |
| 首页 | 去掉全屏视频背景，改为与工作台一致的浅色封面 |
| Doctor | Viewer Gateway 为 Doctor HTML 注入共享主题，随深夜 / 浅色偏好切换 |

边界：

- 这是展示层补丁，不改变 Doctor 检查项、数据读取、转换或任何质量判定；
- 上游业务逻辑与组件结构不变，升级 letools 时需要重新核对补丁是否仍能干净应用；
- 报告中 Doctor 的逐条明细仍保留英文原文，便于与上游工具对照；
- `--no-annotations` 仍然生效，标注页只做本地会话内编辑，不写回数据集；
- Doctor 标签页里的报告由 letools 以硬编码深色样式渲染，因此宿主在
  `pipeline/server/viewer.py` 的 Viewer Gateway 中给 `/_data/doctor` 注入共享主题，
  不 fork 固定版本工具链。该路由只在 Viewer 进程内生效，改动后需重启 `bodypipeline-viewer`。

## 4. 与上游协作

新增机器人时，先确认以下事实，再决定是否扩展 letools：

1. 输入是否已有上游 source provider；
2. episode 顺序、索引和 schema 是否稳定；
3. 图像、视频、Parquet 的媒体描述是否完整；
4. state / action 特征名称、维度和单位是否可表达；
5. `letools validate --deep` 是否能读取输出；
6. Visualizer 是否能显示每路相机和关键 feature。

如果 letools 不支持新输入：

- 优先在 BodyPipeline Server 中增加独立 Provider / staging adapter；
- 必要时向上游提交通用 provider 或 bug fix；
- 不要让 `pipeline/web` 或采集页面出现机器人专用分支；
- 不要把“能读文件”误当成“state / action 语义已通过”。

## 5. 升级策略

升级 letools 时必须：

- 固定新 commit，而不是使用浮动 `main`；
- 更新 `jobs.py` 中的幂等工具版本；
- 更新 `environments/letools/uv.lock`；
- 重新运行 v2.1 / v3.0 转换、deep validate、Doctor 和 Visualizer；
- 比较 Doctor schema 和必需检查项；
- 用至少一个 AgileX 原始 episode 做完整回归；
- 记录未兼容的旧数据集和报告；
- 重新应用 `patches/letools-visualizer/0002-zh-cn-light-theme.patch`，确认它仍能干净应用。

不要在运行中的任务上替换工具环境；先排空任务、停止 Server，再同步依赖。

## 6. Episode 回收站

Viewer 侧边栏垃圾箱按钮位于标记按钮左侧，数据集回收站地址为
`/bp/<preview-id>/trash`。软删除状态按数据集绝对路径保存至
`state/recycle/`，因此多个预览链接共享同一个回收站，重新打开预览也不会丢失。
支持批量恢复与批量永久清理；所有写操作沿用登录认证与同源 CSRF 检查，
并要求提交列表 revision，避免误操作已经重新编号的 Episode。

永久清理由 letools Python 环境中的 `pipeline.server.recycle_worker` 执行：
保留其他 Episode，重写共享 Parquet 和受影响视频，更新计数、统计、拆分和
连续索引，完成 deep validate 后用 `renameat2(RENAME_EXCHANGE)` 原子发布，
最后删除旧数据。未受影响视频沿用原文件；跨 GOP 的剪切需要重新编码 H.264。
清理对象仅为当前转换数据集，不追溯删除原始采集目录或其他副本。
全部清空也保留有效的空数据集元数据。

同一数据集的回收站修改和清理使用跨进程文件锁。持久化日志及数据集内的
`.bodypipeline-recycle.json` 标记区分发布前后中断：发布前保留原数据，
发布后继续移除旧数据并完成状态更新。清理过程中检测到数据被其他进程修改
则取消发布。预览缓存随数据版本失效，剩余回收站项目与标记同步重编号。

验证：`tests/test_recycle.py` 测试 API、权限、路径约束及持久化；
`tests/fixtures/recycle_scenario.py` 在独立 letools 环境中验证共享数据/视频的
实际删除、非关键帧边界、全部清空，以及发布前后中断恢复。

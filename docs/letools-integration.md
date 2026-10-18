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

### 3.5 Visualizer

Server 启动：

```bash
<letools_bin> visualizer serve <dataset> \
  --host 127.0.0.1 \
  --port 3100 \
  --data-port 8765 \
  --public-data-url <viewer_origin>/_data \
  --no-annotations \
  --production \
  --cache-dir <server_root>/viewer-cache
```

要求：

- Bun 1.3.9 位于 `viewer_bun` 或服务 `PATH`；
- UI 和数据桥只监听 loopback；
- Viewer Gateway 代理 UI、数据 API 和视频 Range；
- 只读、关闭 annotation 后端；
- 每次只允许一个活动数据集；
- 默认 30 分钟过期；
- Viewer 入口不鉴权，部署时必须限制网络访问。

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
- 记录未兼容的旧数据集和报告。

不要在运行中的任务上替换工具环境；先排空任务、停止 Server，再同步依赖。

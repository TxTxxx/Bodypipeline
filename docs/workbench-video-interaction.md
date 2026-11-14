# 工作区视频交互修订（BODY-2，2026-11-15）

本轮仅修改视频播放、拖动与准确逐帧定位；保留标注、审核、筛选、尝试关联、候选与交付语义。使用任意数量的相机，不增加机器人型号分支。

## 实现

- `viewer_ui/workbench-media.ts`：HTML 视频负责播放和拖动。每相机最多一个进行中的原生 seek 和一个可替换的最新目标；输入通过 animation frame 合并，松手不重复 seek。播放头独立更新，不重建时间轴片段或干扰编辑手势。
- 支持 `requestVideoFrameCallback` 时用实际呈现的 `mediaTime`，扣除各相机 Episode 起始偏移后映射统一数据帧。定位到帧显示区间内部以避免浮点边界误选前帧。全部相机有准确呈现证据才声明对齐；请求目标与已显示位置分开。
- 拖动不请求 PNG，也不以旧静态图遮挡视频。暂停和逐帧优先使用同一个视频解码器；无呈现回调或缺少准确证据时，才使用精确 PNG 后备。后备图显示时不接受底下视频的晚到回调改写其帧号。
- 精确后备由每个 app 中一个惰性隔离 CPU 解码进程串行处理，复用有界元数据与容器缓存。等待队列丢弃断开的请求，同帧去重；已派发请求必须读取完响应再释放协议锁。缓存独立于源目录，版本签名参与键和视频 Range 读取校验。
- 切换来源立即停止旧视频、取消回调和取帧、卸载媒体；异步返回绑定原 Session。过期的 play Promise 不得改变新的播放意图。当前帧标记/观察/切分需先准确落在显示帧上。

## 复跑

```bash
.runtime/letools-venv/bin/python tests/fixtures/workbench_video_scenario.py .runtime/new-video-fixture
.venv/bin/python tests/fixtures/workbench_video_server.py .runtime/new-video-fixture --port 18132
```

打开 `http://127.0.0.1:18132/workbench?dataset=video-fixture&episode=1`。
该 fixture 为三相机、30fps、三条 Episode 共用各自视频文件，相机起始额外偏移分别为 0、7、19 帧，数据时间戳另含非零起点。实际 H.264 有 B 帧与跨帧编码；图内烧录 Episode、相机、帧号与可机器读取的像素码。

验收服务只在独立目录保存状态，且不启动 AI 或原 Viewer。可选只读登记本机 `robot_screw_04`，不写源数据，不使用用户现有配置或服务。

## 实际验收（已完成）

- 原实现相同 24 步鼠标拖动：147 个 PNG 请求，峰值 126 个未返回；松手两秒后仍未完成定位。修复后的三份素材在随机定位、拖动、播放/暂停、连续逐帧、首末帧期间均为零 PNG 请求；独立像素对照请求另计。
- 三相机带帧号 fixture：首末帧和每次停帧读取实际视频像素码，同一次数据帧三路一致，各相机 0/7/19 帧偏移及共享视频正确。
- 只读抽查 `robot_screw_04`：Episode 0 为 302 帧/30fps；Episode 1 为 820 帧，三路复用各自同一视频文件，从 10.0666667 秒开始。三路暂停末帧 PTS 与独立 PyAV 解码完全对应，平均 RGB 绝对误差约 0.70–0.92/255（浏览器与 PyAV 色彩转换差异）。
- 实际浏览器快速 play/seek/切数据集只保留最终目标；关闭呈现回调能力后，真实 PNG 后备可显示并准确对齐，继续拖动立即撤下旧图。TS 另覆盖后备图上的晚到视频回调、旧 play Promise 拒绝及播放后回到先前位置。
- 实际编辑回归保留文本输入快捷键保护、共享边界联动、撤销/重做、框选/标尺手势、离线跨 Episode 输入与重连同步。纯浏览前后标注文档与修订不变。
- 选定的真实元数据、Parquet 与三路共享视频共 6 个文件、29,238,103 字节，验收前后 SHA-256、大小、mtime 完全一致。全部写入仅在独立验收目录。

| 素材 | 三路拖动反馈 p50 / p95 | 连续逐帧 p50 / p95 | 交互 PNG |
| --- | --- | --- | --- |
| 带帧号三相机 | 14 / 18 ms | 50 / 53 ms | 0 |
| 真实 Episode 0 | 33 / 81 ms | 50 / 51 ms | 0 |
| 真实 Episode 1 | 33 / 99 ms | 50 / 67 ms | 0 |

口径：拖动反馈从 slider input 到所有相机实际呈现该位置，25 个合成输入均呈现；真实两段各 24 个输入、21 个位置呈现，3 个过期中间位置合并，不计入延迟样本。松手后的最终目标三路全部准确对齐。每段另有 8 次随机定位（p50 约 41 ms，p95 约 47–74 ms）和 12 次连续前进逐帧。分位数按已捕获原始事件线性插值。

新建页面/视频元素直到首次对齐实测约 348–405 ms，包括浏览器 CLI 操作开销。没有清除 OS 页缓存，素材此前已读过；这是本机 loopback、CPU、当前 Chromium 的样本，不是冷磁盘或跨网络性能保证。关闭回调后的 PNG 后备在独立验收 PNG 缓存清空时首次 159 ms、同位置热读 116.5 ms（各一次，包括兼容等待时间，进程/媒体容器已热），并复用同一个解码进程。未承诺零延迟。

可复跑的真实浏览器用例：

```bash
.venv/bin/python tests/fixtures/workbench_video_probe.py --case native
.venv/bin/python tests/fixtures/workbench_video_probe.py --case real
.venv/bin/python tests/fixtures/workbench_video_probe.py --case fallback
.venv/bin/python tests/fixtures/workbench_video_probe.py --case races
.venv/bin/python tests/fixtures/workbench_video_probe.py --case editing
.venv/bin/python tests/fixtures/workbench_video_probe.py --case showcase
```

`native` 与 `real` 记录原始时间、请求和像素；`editing` 只修改合成数据的独立标注头。`showcase` 录制不带注入光标的干净短视频；不要使用 `record --cursor`，其注入层在严格 CSP 页面可能遮挡画面。录制需现有 CPU ffmpeg 工具链（probe 会设置其 PATH 和动态库路径），产品视频读取本身不要求外部 ffmpeg 命令。

检查：相关 Python 78 项通过；TS/Bun 全套 72 项通过；工作区严格 TypeScript、JS 语法、生成包一致性、新改 Python 格式、`ruff check pipeline scripts tests` 与 `git diff --check` 均通过。`ruff check .` 的旧 Argus `0001` 补丁有两处未使用导入，在未改动的修复前 `b2b07dc` 原样复现；未越界改它。此前全套的 15 个既有失败不涉及本次，按任务要求没有重复全套。

## 生效方式

工作区 JS/CSS 由 Server 直接读文件，已运行在此 worktree 的试用页可强制刷新获得前端交互修复。复用精确解码器的 Python 后端须由用户重启其 Server/Gateway 后生效。视频修复验收阶段仅启停独立验收服务，未操作用户的 18122。后续用户明确授权本机 AI 配置，已按原 root 受控重启这一份试用服务，Python 解码优化现已加载；启动/停止方式见 [本机 AI 标注试用](local-trial.md)。

## 未证明范围

- 当前 Chromium、本机 CPU/loopback 之外的设备、浏览器、网络与更长/更高分辨率/其他编码的性能；手机仅核验 Proof 排版，不等于手机编辑器交互已经实测。
- 严格的每次播放显示帧锁定：连续播放作为浏览预览，使用实际呈现时钟和漂移调整；暂停/微调及当前帧落标要求三路准确对齐。
- 真实模型质量、机器人或 GPU 及部署；本轮没有模型调用、新凭据或服务部署。

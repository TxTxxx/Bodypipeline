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

## 验证进度

- 原实现相同三相机实际鼠标拖动：147 个 PNG 请求，峰值 126 个未返回；松手两秒后仍未完成定位。原始浏览器记录与短视频保存于本轮 Proof 素材目录。
- 已通过：TS 视频时钟/去重/旧回调与现有编辑模型回归 16 项；Python 媒体/工作区回归 17 项。Python 实际解码三相机首末帧与跨 Episode 偏移，校验图内像素、同帧并发去重、取消队列、崩溃恢复与源哈希不变。
- 真实浏览器全面交互、延迟统计、真实数据 Episode 0/1 及 Proof 更新仍在进行。此处不把函数回归视为实际交互验收。

## 生效方式

工作区 JS/CSS 由 Server 直接读文件，已运行在此 worktree 的试用页可强制刷新获得前端交互修复。复用精确解码器的 Python 后端须由用户重启其 Server/Gateway 后生效。本任务只启停自己的独立验收服务，不操作用户的 18122 服务。

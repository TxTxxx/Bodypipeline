# AgileX ALOHA recorder 启动时间空洞修复

本文记录 AgileX ALOHA Legacy recorder 在每次 start 后产生关节时间空洞的根因、修复和回归。修复不改变 Server 的 `20 ms / 50 ms` 时间对齐阈值，也不裁剪、插值或删除已录制文件。

## 1. 现象

多段主从臂采集在开头约 0.3 秒内出现关节空洞。Server 批量转换返回：

```text
arm/jointState/puppetLeft frame=5 error=70.115ms limit=50.000ms
```

历史样本 `e8d4ada4…`、`aa13689a…`、`d56f9753…` 均在开头存在独立空洞。旧版任务执行器在第一个 episode 异常后终止，导致页面只显示第一条错误，掩盖了后续 episodes 的结果。

## 2. 根因

正式 recorder 录制期间，同时使用独立只读订阅观察四路关节 topic：

| 观察项 | 结果 |
| --- | --- |
| 正式 recorder 的 `puppetLeft` 文件 | 出现 `114.927 ms` 空洞 |
| 同一时间窗口的旁路订阅 | 连续收到 22 帧关节消息 |
| 旁路四路关节最大 header 间隔 | 低于 50 ms |
| 正式 recorder 关节回调耗时 | 最长约 `0.188 ms` |
| 回调帧数与文件数 | 一致 |
| 跳过 `CameraInfo` 配置回调后 | 仍可复现 |

因此可以排除：

- 机械臂驱动停止发布；
- DDS 全局丢包；
- Client 前端、浏览器或 SSH 延迟；
- Server 转换算力；
- 关节回调阻塞或文件写入丢失。

问题位于 recorder：每次 start 才创建订阅，订阅发现和可靠历史补发尚未稳定时就开始有效 episode。启动阶段收到压缩历史批次和跳跃时间戳，随后流才恢复连续，这些跳跃被原样写入文件。

## 3. 修复内容

补丁：

```text
patches/agilex-data-tools/0001-recorder-stream-stability.patch
```

修复要点：

1. recorder 启动后先让订阅在 executor 中运行，再进行就绪门控。
2. 三路彩色相机分别要求连续 15 个不超过 50 ms 的时间间隔。
3. 四路必需关节分别要求连续 25 个不超过 50 ms 的时间间隔。
4. 就绪后清空预热队列、计数器和变化检测状态，再开始写 episode。
5. ROS service 只在门控通过后返回成功；超过 8 秒未稳定则拒绝启动。
6. 相机与关节使用独立 `ReentrantCallbackGroup`。
7. 删除彩色相机回调中未使用但仍执行图像复制的 `cv_bridge::toCvCopy()`。

这里等待的是稳定后的新数据，不是按固定时长裁剪旧数据。磁盘上的 episode 仍保留完整时间戳。

## 4. 部署与回滚

在 AgileX ALOHA 上执行：

```bash
cd /opt/bodypipeline
python3 scripts/apply_agilex_recorder_fix.py \
  --data-ros /path/to/robot_ws

# dry-run 通过后
python3 scripts/apply_agilex_recorder_fix.py \
  --data-ros /path/to/robot_ws \
  --apply --build
```

确认没有录制会话后重启：

```bash
systemctl --user restart bodypipeline-recorder
```

现场部署备份：

```text
<backup-root>/recorder-stream-stability-<timestamp>/
```

备份包含原 `dataCapture.cpp`、`blockingDeque.h` 和安装后的 recorder 二进制。

## 5. 回归结果

修复版先以诊断二进制完成 3 次 5 秒静态采集，再替换正式 recorder 完成 4 次：

| 样本 | 四路关节最大间隔 | 三路相机最大间隔 | 超过 50 ms |
| --- | ---: | ---: | ---: |
| `sample-01` | 15.095 ms | 36.070 ms | 0 |
| `sample-02` | 10.667 ms | 36.130 ms | 0 |
| `sample-03` | 15.994 ms | 36.096 ms | 0 |
| `sample-04` | 10.858 ms | 36.091 ms | 0 |

第 4 条是在 Client / Bridge 上线“首条 CaptureStatus 后才标记 RECORDING”后的完整链路回归。诊断二进制与正式二进制合计 7/7 次通过。

测试没有发送机械臂运动命令，样本保持 `REVIEW`，没有上传为训练数据。静态回归不能替代真实遥操作动作、夹爪标定和完整训练数据验收。

## 6. 批量任务诊断

Server 批量转换现在会继续核验所有 episodes，并在 `episode_diagnostics` 中记录逐条结果：

```json
{
  "episode_index": 0,
  "upload_id": "...",
  "passed": false,
  "error": "arm/jointState/puppetLeft frame=5 error=70.115ms limit=50.000ms"
}
```

一个 episode 失败不会掩盖其他 episodes 的具体错误，但整个任务不会生成正式可预览数据集。

## 7. 现场检查

```bash
systemctl --user is-active \
  bodypipeline-recorder \
  bodypipeline-bridge \
  bodypipeline-client \
  bodypipeline-upload-tunnel

curl -sS \
  -H "Authorization: Bearer <client-token>" \
  http://127.0.0.1:8080/api/health
```

检查新 episode 的关节最大相邻间隔：

```bash
python3 - <<'PY'
from pathlib import Path
root = Path("/path/to/episode0/arm/jointState/puppetLeft")
stamps = sorted(float(p.stem) for p in root.glob("*.json"))
print(max((b - a) * 1000 for a, b in zip(stamps, stamps[1:])))
PY
```

预期明显低于 50 ms。若再次越界，应保留原始数据并检查 recorder 日志和启动门控，不能直接放宽 Server 阈值。

## 8. 适用范围

该修复只适用于 AgileX ALOHA 当前 Legacy recorder 和 `/path/to/robot_ws`。其他机器人不能直接套用：

- 先确认自己的 recorder 是否也有启动订阅瞬态；
- 记录 service start、首帧写入和稳定数据之间的时序；
- 新平台实现自己的预热门控与回归样本；
- 保留统一的有界时间对齐策略，不用插值掩盖真实空洞。

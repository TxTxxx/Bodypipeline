# ARX X5 Client

`arx-x5` 是机器人侧 Client 的独立分支，基于 2026-10-26 部署的功能整理。
本分支包含 Client、ROS2 Bridge、公共依赖和通用配置示例，不包含中心 Server。
现场配置、部署记录、账号凭据、相机序列号、数据及外部运行环境均未包含。

## 功能

- 实体按键连续采集：`data[0]` 开始，`data[1]` 回位保存。
- 三路相机与双臂消息独立保存 raw，后台进程按源时间轴转换 HDF5。
- 持久化目录选择，录制中选择新目录从下一段生效。
- 下一段开始后确认上一段上传，转换完成后通过 SSH/rsync 传输。
- 转换失败可手动重新转换并上传，上传失败自动重试。
- 本地及远端回收站删除，页面保留已删除记录。
- Enactive 推理控制、采集/推理模式交接、相机和关节监控。
- DAgger 纠偏包导入、转换、上传与删除。
- 点击 `download` 后按需生成三路拼接 MP4。HTTP 下由浏览器直接下载；
  HTTPS / localhost 且浏览器支持时，可先选择本机保存位置。

## 目录

```text
pipeline/client/  Client API、ROS2 Bridge、采集、转换、上传、推理、视频导出
pipeline/shared/ 配置、状态、鉴权和数据格式
pipeline/web/    Client 页面与共用静态资源
scripts/         raw 转换、Bridge 启动、CAN 与上传回收辅助程序
configs/         通用配置示例与机器人 Profile
```

Client 的入口为 `pipeline/client/app.py`，Bridge 为 `pipeline/client/arx_bridge.py`。
机器人差异由 Adapter、Provider 和 Profile 适配。

## 启动

Client 使用 Python 3.10+，Bridge 使用已配置 ROS2 的独立 Python 环境。
外部依赖包括机器人 ROS2 控制器、相机驱动、Enactive / Policy 服务；这些运行时不随本仓库分发。
常驻控制模式要求现场控制器支持 `v2_workbench.launch.py` 及模式交接服务。
视频下载依赖 FFmpeg 的 `libx264`，上传依赖 `ssh` 和 `rsync`。

```bash
uv sync --locked --no-dev
cp .env.example .env
```

修改 `.env` 中的占位凭据，并配置 `configs/client.arx-x5.json` 中的工程路径、
相机序列号、上传主机、用户与 SSH 文件位置。示例配置中的值均需按实际部署填写。
分别在 Client 和 Bridge 终端加载同一环境文件：

```bash
set -a
source .env
set +a
```

启动 Bridge：

```bash
bash scripts/run_arx_bridge.sh configs/client.arx-x5.json
```

启动 Client：

```bash
.venv/bin/python -m pipeline.cli client --config configs/client.arx-x5.json --host 0.0.0.0 --port 10866
```

示例中的 Bridge 仅监听本机 10867。打开页面不会自动开始录制或推理，硬件启动由显式使能操作触发。

## 数据行为

普通数据按 `<采集根>/<任务>/<数采员>/<日期>/episodeN.raw` 和同编号 HDF5 保存。
普通 raw 为 SQLite 消息容器；DAgger raw 为来自执行运行时的 ZIP bundle。
raw-first 以 head 源帧为参考，其他流采用最近消息，允许复用并在报告中记录。
源时钟需在部署时核实，上游已丢失的消息不能通过转换恢复。

普通示教 action 是同帧实测位置，DAgger action 是下一条实测位置并带有效性掩码。
训练读取时应区分格式。

SSH 上传保留相对目录，只上传 HDF5，本地保留 raw；上传完成不代表中心侧质检完成。
视频仅在点击下载时生成，保存到打开浏览器的电脑。

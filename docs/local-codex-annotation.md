# 本机 Codex 自动标注

在自动标注页将「标注后端 / 执行方式」选为「本机 Codex」，选择中/英文任务模板、已登记数据集与运行范围，填写 Episode 编号后提交。默认每秒采样三帧、并发一条，可在页面调整；试跑不会自动继续全量。两份英文模板与中文模板使用同一套模板契约，英文正式标签和观察 notes 遵循模板的英文要求。

「本机」指 Codex CLI 在运行 Server 的机器上启动；模型计算可能由 Codex 服务完成，并非本机 GPU 推理。复用该服务器用户的 CLI 登录及公开模型/提供方配置，无需填写标注 API 网关或 API Key。登录由用户在服务器上运行 `codex login` 完成；服务不会读取、复制或展示认证文件或密钥值。

首次交付支持**模板模式**。自由分析仍使用 API / Argus；Codex 模式只用相机证据，原生证据检查、模型字段解释与输出 token 上限不能从此入口设置，页面禁用这些选项，API 拒绝不支持的参数。CLI 模型列表来自本机默认模型或受信部署允许列表，不调用标注网关。可选择的推理强度会明确传入 CLI；模型拒绝某种设置时任务显示配置错误。

任务列表记录执行后端、有效模型、采样率、模板快照与源签名。旧任务没有后端字段时仍按 API / Argus 解释。续跑保留成功 Episode/part，并且不跨后端切换；模型提供方、执行路径或输入限制改变时要求创建新任务。完成的模板候选可从结果页送到标注工作区，再按现有流程应用 AI 草稿、人工核对与导出；AI 草稿不是人工确认。

同一逻辑请求最多首次加三次 **CLI 调用**，计数持久化，格式错误、显式续跑与服务重启共享这个计数。CLI 内部连接恢复由 Codex 管理。认证、额度或配置错误不会自动重发；修复后可明确续跑，仍受累计调用次数约束。取消或重启时，已经收到的结果保留，未派发项可继续；在途结果未知的调用不会盲目重发，需要核对后明确创建新的重算任务。取消只终止本任务的进程组。

每次调用保留本地调用 ID、CLI 会话 ID、状态、退出码和 CLI 报告的 token 用量。实际美元金额无法核实，显示「未知」，`cost_usd=null`、`cost_complete=false`；未收到用量时也不推算费用。标注不限额时，任务、每日和 Episode 上限均为空，不以假零元账单代替实际价格。跳过模型分辨率路由是确定未发起调用，单独记零成本；它不是 Codex 推理费用。

## 部署

Server 和已有 Argus/CPU 媒体工具链必须可用。新安装使用 `scripts/install_argus.sh`，它将补丁 0001–0005 及受控 transport 一起安装。更新现有部署时应在自己的 Argus 副本应用补丁并提交，保持工具源码干净；不要覆盖其他任务的 vendor。无需 GPU、Slurm 或重新安装训练环境。

受信 Server 配置可设置：

```json
{
  "argus_annotation_unlimited": true,
  "codex_bin": "/absolute/path/to/codex",
  "codex_model": "",
  "codex_models": [],
  "codex_reasoning": "",
  "codex_timeout": 1800,
  "codex_max_images": 96,
  "codex_max_image_bytes": 33554432,
  "codex_max_prompt_chars": 180000,
  "codex_max_output_bytes": 2097152
  ,"codex_max_part_seconds": 60.0
}
```

空模型和推理强度复用 CLI 的公开默认值；空允许列表仅提供默认模型。可执行文件只接受部署配置，HTTP 请求不能传命令。输入复用现有长 Episode 分 part 流程：默认最多六十秒，并按图像数量、字节预算、采样率和实际相机数继续收紧；边界细化也按实际相机数缩小批次。不会偷偷降低 3 Hz 或忽略相机。异常超大证据仍会明确失败，已完成的部分保留。所有相机与时间来自源数据。

CLI 每次在独立临时输入目录运行，使用 `--image`、`--output-schema`、`--json`、`--output-last-message`、只读 sandbox。模板和边界细化使用各自的 JSON Schema，直接约束业务对象、正式 ID 和字段作用范围，随后仍通过现有模板业务校验。只复用模型/提供方配置；关闭 shell、插件、MCP、浏览器、技能、委派、全局项目指令与 hooks，不把它当编码 agent。原始 CLI stderr 和事件不直接发布到日志，以免泄漏上游配置。

`scripts/local_trial.py` 保留必要的系统网络代理（包括大小写的 HTTP/HTTPS/ALL/NO_PROXY）及 `CODEX_HOME`，继续隔离标注网关密钥。传给 systemd 的参数只包含环境变量名，不将代理值放在命令行。切换代码前，入口检查活动任务；复用原绝对状态根与数据登记，不重建状态、不迁移旧快照。

本次本机托管服务使用本工作树的 `.runtime/body4-local-trial.json`，绝对状态根继续指向原本机试用状态。管理命令：

```sh
python scripts/local_trial.py status --config .runtime/body4-local-trial.json --port 18122
python scripts/local_trial.py check --config .runtime/body4-local-trial.json --port 18122
python scripts/local_trial.py restart --config .runtime/body4-local-trial.json --port 18122
```

## 针对性验证

```sh
python -m pytest tests/test_codex_annotations.py tests/test_codex_transport.py tests/test_local_trial.py -q
```

`tests/test_codex_batch.py` 还可在已有 CPU 工具链下运行真实 Argus 队列、微型视频和受控 CLI 的批量/取消/恢复验收。先在隔离目录生成三条两相机四秒视频（不接触用户数据）：

```sh
.runtime/letools-venv/bin/python -c 'import sys; from pathlib import Path; sys.path.insert(0,"tests/fixtures"); from workbench_scenario import create; create(Path(".runtime/codex-batch-fixture"),count=3)'
python -m pytest tests/test_codex_batch.py -q
```

受控 CLI 测试只证明队列及故障行为；真实账号、图像推理和英文候选另由页面提交的小样本任务验证。没有验证全量吞吐、识别率或边界达到 ±0.2 秒。

CLI 选项依据 [OpenAI Docs：Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)、[Developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli) 和 [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)，同时以运行机器的 CLI 帮助及真实 canary 为准。

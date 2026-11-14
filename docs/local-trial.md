# 本机 AI 标注试用

从任务 worktree 运行（Linux 用户级 systemd，无需 sudo）：

```bash
.venv/bin/python scripts/local_trial.py start
```

打开 <http://127.0.0.1:18122/#annotations>。已有 `.runtime/body2-local-trial.json` 和其 `root` 保留；入口只补缺失的本地工具路径与默认并发，不重装/重置 Argus，不导入或改写数据，不自动提交模型调用。重复 `start` 保持已运行服务；本机端口被其他进程占用时拒绝接管。

`argus_src` 是含 `review/`、`label/` 的本机 Argus 源码目录，当前复用 `.runtime/vendor/argus`；`argus_python` 指向 `.runtime/argus-venv/bin/python`，保留虚拟环境路径而不解析到系统解释器。启动前核验源码干净、模板/执行契约 v2、实际模块导入和 CLI 帮助，并确认 CPU ffmpeg/ffprobe 可运行。脚本配置其 PATH、FFMPEG/FFPROBE 和动态库路径，无需默认 PATH 中有 uv/ffmpeg，也不会使用 GPU。

服务交给用户级 systemd 运行，终端退出后继续可用；没有设开机自启动。管理命令：

```bash
.venv/bin/python scripts/local_trial.py status
.venv/bin/python scripts/local_trial.py check
.venv/bin/python scripts/local_trial.py restart
.venv/bin/python scripts/local_trial.py stop
tail -f .runtime/body2-local-trial/reports/local-trial.log
```

单元名由配置文件路径稳定生成，`status` 会显示它及主进程。只停止此入口创建的单元；有活动的标注、转换、分析或检查任务时拒绝停止。`restart` 先完成配置与工具预检，失败则保留正在运行的服务。独立状态目录的写锁在启动前检查，避免旧进程尚未关闭完就抢占。

本机试用只监听 `127.0.0.1`，要求现有配置 `trusted_network=true`。临时设备鉴权密钥仅在服务进程内生成，不写配置或日志；入口不继承终端/agent/用户服务管理器中的模型密钥。模型 API Key 可直接在产品页面的密码框填写，仅用于本次任务的内存凭据；无需交给 agent 或写入文件。

## 本轮已配置与仍需填写

已按用户提供的公开参数设置本机运行配置：模型网关及 `gpt-6.1-sol` 默认模型；API 返回 Argus、模板、执行契约 v2 和网关配置均可用。服务器未配置默认模型 Key，`api_key_configured=false` 如实保留。选择数据集和合适模板，先用 Episode 0、并发 1，由用户提交试跑；没有自动推理或自动全量。

按用户要求，当前本机配置已设 `argus_annotation_unlimited=true`：单任务、每日和每 Episode 费用上限均取消，API 返回 `unlimited=true`、三个默认上限均为 `null`，页面显示不限额，无需填写费用上限。这只改变当前试用配置，产品默认值不变；试跑范围、并发、取消/恢复、输出长度和最多三次自动重试仍有效。

费用估算、用量和运行记录继续保留。`gpt-6.1-sol` 的 Argus 内置计价是软件估算依据，不代表此代理的实际报价；若代理另有价格，可在运行配置 `argus_prices` 按 USD/token 填入真实标准/长上下文档位。不限额模式不会因缺少价格拒绝创建任务，未知费用仍显示为未知，不能当作零费用。模型列表需页面填写 Key 后才可向网关查询；本轮没有携带凭据进行认证查询或发起推理。

验证：此前启动入口与已有标注 API 相关回归 68 项通过；本次不限额回归通过，覆盖三类费用上限、未知模型价格与续跑费用记录。取消上限的受控重启前后，两份数据登记、一份模板库、一个标注头和 26 条历史记录共 30 个文件哈希完全一致。随后只追加新模板，原模板及已有标注保持原样。当前视频 Python 优化已随受控重启加载。真实模型结果、代理实际费用与其视觉能力尚未由本轮验证；用户后续自行提交的任务另计。

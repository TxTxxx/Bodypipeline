# 发布与回滚 Runbook

本 Runbook 以 Linux systemd user service 和不可变 release 目录为例。实际路径、service 名和命令按部署环境调整。

## 1. 发布前变量

```bash
export APP_ROOT=/srv/bodypipeline
export RELEASES="$APP_ROOT/releases"
export CURRENT="$APP_ROOT/current"
export RELEASE_ID="$(git rev-parse --short=12 HEAD)"
export CANDIDATE="$RELEASES/$RELEASE_ID"
export SERVICE=bodypipeline-server
```

要求：

- `RELEASE_ID` 对应已审查 commit；
- 工作树干净；
- 候选目录尚不存在；
- 旧 release 和 `current` 指针已记录；
- 已取得明确部署授权。

## 2. 预检

```bash
git status --porcelain
python3 agents/skills/deploy-robot-adapter/scripts/release_guard.py \
  --base "$(git rev-parse HEAD^)" \
  --head HEAD \
  --mode isolated-server \
  --instance bodypipeline-newrobot \
  --target-robot-id robot-002 \
  --fleet agents/skills/deploy-robot-adapter/assets/fleet.example.json
```

真实部署不能使用 `--skip-checks` 或 `--allow-dirty`。保存脚本输出的 JSON 报告。

## 3. 备份

```bash
BACKUP="$APP_ROOT/backups/release-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
readlink -f "$CURRENT" > "$BACKUP/previous-release.txt"
cp -a "$APP_ROOT/config" "$BACKUP/config"
systemctl --user cat "$SERVICE" > "$BACKUP/$SERVICE.unit.txt" 2>/dev/null || true
```

按实际环境额外备份：

- `root/state`；
- `data_root/raw` 和 `data_root/work`；
- Viewer cache；
- letools lock / venv 摘要；
- 反向代理和隧道配置。

不要为了备份停止机器人驱动或相机。

## 4. 构建候选 release

从 commit 导出到新目录，不使用 `git pull` 覆盖运行目录：

```bash
test ! -e "$CANDIDATE"
mkdir -p "$CANDIDATE"
git archive HEAD | tar -x -C "$CANDIDATE"
```

安装依赖：

```bash
cd "$CANDIDATE"
uv sync --locked --no-dev
```

如需独立工具链：

```bash
bash scripts/install_letools.sh
```

安装后记录 lock 摘要、letools commit、Bun 路径和 Python 版本。

## 5. 独立实例启动

独立 Server 使用独立配置文件、环境文件和 service：

```bash
systemctl --user daemon-reload
systemctl --user start bodypipeline-server-newrobot
systemctl --user status bodypipeline-server-newrobot
```

先启动服务，再切换目标 Client 的 `server_url`；不要让任何现有 Client 指向候选实例。

检查：

```bash
curl -sS http://127.0.0.1:8181/healthz
curl -sS -H "Authorization: Bearer <admin-token>" \
  http://127.0.0.1:8181/api/config
```

检查 `/api/config` 中的 Profile、`data_root`、`import_roots`、Viewer 地址和工具可用性。

## 6. 金丝雀验收

目标机器人或离线 fixture 至少完成：

1. 设备认证；
2. 一次上传；
3. collection 分组；
4. 选择 episodes；
5. 转换；
6. 结构检查 / deep validate；
7. Doctor；
8. Visualizer。

保存：

- upload ID；
- collection ID；
- job ID；
- dataset 路径；
- Doctor 报告路径；
- Viewer 会话结果；
- 服务日志时间范围。

同时验证旧实例：

```bash
curl -sS http://127.0.0.1:8081/healthz
systemctl --user is-active bodypipeline-server
systemctl --user is-active bodypipeline-viewer
```

旧实例的上传数、任务数和结果路径不应因新实例部署发生变化。

## 7. 共享模式切换

只有 `shared-additive` 已签核时执行：

```bash
ln -s "$CANDIDATE" "$APP_ROOT/current.next"
mv -Tf "$APP_ROOT/current.next" "$CURRENT"
systemctl --user restart "$SERVICE"
```

如果 systemd 使用固定绝对 release 路径而不是 `current` symlink，必须同时更新 unit 并重新加载；不要仅修改一个正在运行进程的文件。

切换后：

- 检查目标机器人；
- 逐台检查全部现有机器人；
- 检查任务队列没有被自动重启；
- 检查现有 `RUNNING` 任务状态；
- 检查 Viewer 是否只停止该实例自己的会话；
- 检查没有读写错 `data_root`。

## 8. 回滚

代码和配置回滚优先：

```bash
ln -s "$(cat "$BACKUP/previous-release.txt")" "$APP_ROOT/current.next"
mv -Tf "$APP_ROOT/current.next" "$CURRENT"
systemctl --user restart "$SERVICE"
```

独立实例回滚：

```bash
systemctl --user stop bodypipeline-server-newrobot
# 将目标 Client 的 server_url 指回旧地址
```

如果状态 schema 未改变且部署后没有新写入，可以恢复 `root/state` 备份。若已有新上传或任务：

- 不要覆盖 `state/`；
- 先冻结新写入；
- 保留新数据；
- 使用前向修复或离线合并；
- 重新验证旧数据仍可读。

## 9. 停止条件

出现以下任一情况立即停止发布或回滚：

- 旧机器人健康检查失败；
- 旧设备上传被拒绝或路由到新 Profile；
- 旧任务被自动重新执行或状态改变；
- 新版本读取旧 JSON 报错；
- Viewer 停止旧实例会话；
- 端口、状态根或数据根与现有实例重叠；
- 无法在计划时间内完成全机队验证。

## 10. 部署后记录

复制 `assets/deployment-record.template.md`，记录：

- release commit 和 release 目录；
- 改动文件；
- 隔离模式；
- 目标实例和机器人；
- 预检、金丝雀、现有机器人回归；
- 备份和回滚命令；
- 观察窗口和剩余风险。

没有记录和回滚证据的部署不算完成。

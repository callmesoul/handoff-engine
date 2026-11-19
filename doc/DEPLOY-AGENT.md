# Agent 安装：独立引擎与交接仓

Handoff Engine 和交接数据必须放在两个不同目录：

```text
~/code/handoff-engine          可公开、可升级的引擎
~/code/handoff-store           私有任务/配置仓（名称可自定）
```

不要把 `tasks/`、`events/`、`contracts/`、`artifacts/` 或机器专属 `config/local.yaml` 放入引擎仓。

## Linux / macOS / WSL

```bash
git clone <handoff-engine-url> ~/code/handoff-engine
git clone <private-handoff-store-url> ~/code/handoff-store

python3 -m venv ~/code/handoff-engine/.venv
~/code/handoff-engine/.venv/bin/pip install -e ~/code/handoff-engine

export HANDOFF_ENGINE_ROOT="$HOME/code/handoff-engine"
export HANDOFF_ROOT="$HOME/code/handoff-store"
export PATH="$HANDOFF_ENGINE_ROOT/.venv/bin:$PATH"

handoffctl --root "$HANDOFF_ROOT" doctor --role frontend
```

把三个环境变量写入 shell profile、systemd 环境文件或运行 Agent 的宿主环境。

## Windows PowerShell

```powershell
$EngineRoot = Join-Path $HOME 'code\handoff-engine'
$HandoffRoot = Join-Path $HOME 'code\handoff-store'

git clone <handoff-engine-url> $EngineRoot
git clone <private-handoff-store-url> $HandoffRoot

py -3 -m venv "$EngineRoot\.venv-win"
& "$EngineRoot\.venv-win\Scripts\python.exe" -m pip install -e $EngineRoot

setx HANDOFF_ENGINE_ROOT $EngineRoot
setx HANDOFF_ROOT $HandoffRoot
& "$EngineRoot\.venv-win\Scripts\handoffctl.exe" --root $HandoffRoot doctor --role frontend
```

重新打开终端或 Agent 宿主，让新环境变量生效。

## 安装 skills

Codex/DSH 能发现的 skill 应来自引擎仓，而不是数据仓：

```bash
mkdir -p ~/.agents/skills
ln -sfn ~/code/handoff-engine/.agents/skills/handoff ~/.agents/skills/handoff
ln -sfn ~/code/handoff-engine/.agents/skills/deliver ~/.agents/skills/deliver
```

Windows 可创建目录联接，或把这两个目录复制到宿主的 skills 目录。skill 运行时只读 `HANDOFF_ROOT`，通过已安装的 `handoffctl` 操作交接仓。

## DSH 插件

三个插件都从引擎仓安装：

- `dsh-plugin/`：完成事件回推原会话
- `dsh-handoff-panel/`：嵌入本地任务面板
- `dsh-handoff-studio/`：使用 DSH Agent 执行任务

Handoff Studio 使用 `HANDOFF_ROOT` 寻找私有交接仓；其 Python 环境必须已经安装 `handoff-engine`。不要在 `cordis.patch.yml` 提交本机绝对路径。

## 升级

引擎升级与交接数据同步互不影响：

```bash
git -C ~/code/handoff-engine pull --ff-only
~/code/handoff-engine/.venv/bin/pip install -e ~/code/handoff-engine

# 数据仓只按团队流程同步
git -C "$HANDOFF_ROOT" pull --ff-only
```

升级引擎不会改写交接仓；任务状态变化仍只能通过 `handoffctl` 产生。

## 自检

```bash
handoffctl --help
handoffctl --root "$HANDOFF_ROOT" validate
handoffctl --root "$HANDOFF_ROOT" doctor --role frontend
```

常见问题：

- 找不到 `handoffctl`：确认引擎虚拟环境的 `bin`/`Scripts` 在 PATH，或直接调用其中的可执行文件。
- 找不到交接仓：设置 `HANDOFF_ROOT`；不要回退到引擎目录。
- schema 文件缺失：重新安装引擎。schema 已打包进 wheel，不应从交接仓复制。
- 服务仍跑旧代码：重启对应 worker/notifier/listener，并确认它使用引擎虚拟环境。

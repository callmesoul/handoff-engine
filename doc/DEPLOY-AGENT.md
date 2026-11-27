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

### Windows DSH 打开 WSL 工作区

DSH 宿主是 Windows 进程时，`$env:HANDOFF_ROOT` 可能为空或仍是启动 DSH 时的旧值；这不代表 WSL 发行版里的 login-shell 环境也为空。对 `\\wsl.localhost\<distro>\...` 工作区，应在对应发行版里解析并执行：

```powershell
wsl.exe -d <distro> -- bash -lc 'printenv HANDOFF_ROOT'
wsl.exe -d <distro> -- bash -lc 'handoffctl --root "$HANDOFF_ROOT" doctor --role frontend'
```

不要因 Windows 宿主变量缺失而回退到旧交接仓。若希望原生 Windows 工作区直接使用该变量，仍需 `setx` 后重启 DSH 宿主。

## 安装 skills

Codex/DSH 能发现的 skill 应来自引擎仓，而不是数据仓：

```bash
mkdir -p ~/.agents/skills
ln -sfn ~/code/handoff-engine/.agents/skills/handoff ~/.agents/skills/handoff
ln -sfn ~/code/handoff-engine/.agents/skills/deliver ~/.agents/skills/deliver
```

Windows 可创建目录联接，或把这两个目录复制到宿主的 skills 目录。skill 运行时只读 `HANDOFF_ROOT`，通过已安装的 `handoffctl` 操作交接仓。

## DSH 插件

插件按部署角色安装，不能在前后端无差别全装：

| 插件 | 前端部署 | 后端部署 | 用途 |
|---|---:|---:|---|
| `dsh-plugin/` | 安装 | 不安装 | 完成事件回推原前端会话 |
| `dsh-handoff-panel/` | **安装** | **不安装** | 前端发单、跟踪、验收与最终确认 |
| `dsh-handoff-studio/` | **不安装** | 安装 | 后端专用执行面板，用 DSH Agent 实现任务 |

`dsh-handoff-panel` 固定启动 `handoffctl panel --role frontend`；页面和 HTTP API 都不暴露执行器、自动模式、认领、重试、租约、推进或执行日志等后端操作。后端部署不得安装该插件；历史上装入后端 profile 的应执行 `dsh plugin --profile <profile> remove dsh-handoff-panel`。

Handoff Studio 使用后端机器的 `HANDOFF_ROOT` 寻找私有交接仓；其 Python 环境必须已经安装 `handoff-engine`。不要在 `cordis.patch.yml` 提交本机绝对路径。历史上在前端 profile 安装过 `dsh-handoff-studio` 的，应将它移除。

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

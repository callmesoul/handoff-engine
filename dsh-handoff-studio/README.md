# Handoff Studio

在原 `handoffctl panel` 之外并行运行的一套面板：**执行器换成了 DSH 自己**——用一个
在面板里选定的模型跑任务实现，而不是拉起 `codex` / `zcode` 子进程。

原面板一行没改。两套面板读同一个交接仓，可以同时开着。

## 它做了什么

```text
浏览器里的 DSH 面板（React）
        │  /handoff-studio/panel/*        同源，无 CORS
        ▼
dsh-handoff-studio 插件（DSH 宿主进程）
        │  按需拉起 / 代理
        ▼
Python studio 引擎（127.0.0.1:8791）
        │  复用 handoff 的 worker 管线：认领 → worktree → gate → 提交 → PR
        ▼
   实现执行器 = DSH Agent
        │  POST /handoff-studio/agent/run（Bearer 令牌）
        ▲
插件用面板选的 provider / model / effort 开一个 Agent，跑完把结构化结果交回
```

关键点：**执行器换了，交付流程没换。** 认领、隔离 worktree、路径 gate、提交、推送、
PR、验收依旧是 `handoff` 原来那条链路，所以 studio 跑出来的任务和原面板跑出来的
完成了同一套检查。

## 模型怎么选

面板顶部的选择器列出 DSH 当前**路由得到**的全部模型（`sessionController.modelCatalog()`），
不是写死的清单。选中的模型和思考程度记在 `.handoff-run/panel-state.json` 的 `studio` 键里，
重启面板后仍然生效；下一个任务开始执行时使用。

每个模型支持的思考程度不同：选模型后，档位下拉会跟着换成该模型声明的那组，选了不支持的
档位会被明确拒绝（不会静默夹到别的值）。

## 与原面板的区别

| | 原面板 `handoffctl panel` | Handoff Studio |
| --- | --- | --- |
| 执行器 | `codex` / `zcode` 子进程 | **DSH Agent**（另有 codex/zcode 可选） |
| 模型来源 | `config/*.yaml` + `~/.codex/config.toml` | DSH 宿主实时模型目录 |
| 界面 | 面板自己发的 HTML，DSH 里用 iframe 框 | DSH 原生 React 面板 |
| 端口 | 8790 | 8791 |
| 任务仓 | 同一个 | 同一个 |

两者共用 `config/panel.yaml`，但 studio **不读** 其中的 `default_executor`：那个字段
属于原面板的语义，studio 的默认执行器恒为 `dsh`。

## 路由

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/handoff-studio/health` | 免令牌；`{ ok, name, version, path, panel }` |
| GET | `/handoff-studio/models` | 需要令牌；宿主实时模型目录 |
| GET | `/handoff-studio/presets` | 需要令牌；可用 agent preset 与当前配置值（诊断用） |
| POST | `/handoff-studio/agent/run` | 需要令牌；`{ cwd, prompt, provider?, model?, effort?, resumeSessionId? }` → `202 { runId, sessionId }` |
| GET | `/handoff-studio/agent/run/:id?after=N` | 需要令牌；`{ phase, logs[], cursor, text, error }` |
| POST | `/handoff-studio/agent/run/:id/cancel` | 需要令牌；尽力取消当前这一轮 |
| ANY | `/handoff-studio/panel/*` | 代理到 studio 引擎（`127.0.0.1:8791`），浏览器同源访问 |

同一时刻只跑一个 Agent：handoff 的 worker 本来就是单并发，第二个请求回 `409`。

## 令牌与地址发现

插件首次加载时生成 `$DSH_HOME/handoff-studio.token`（`0600`），并把实际地址写进
`$DSH_HOME/handoff-studio.json`。Python 侧读同样的两个文件，所以两边不需要手工同步
配置，DSH 端口变了也能自动跟上。

查找顺序（Python 侧）：`HANDOFF_STUDIO_URL` 环境变量 → 端点文件 → `DSH_WEB_URL`。
令牌：`HANDOFF_STUDIO_TOKEN` 环境变量 → 令牌文件。

## 配置（可选）

在 profile 的 `cordis.patch.yml` 里给这一行加配置：

```yaml
- id: handoff-studio
  config:
    # 引擎端口；默认 8791（8790 = 原面板，8789 = frontend-listener）
    enginePort: 8791
    # 拉引擎用的 Python；默认 HANDOFF_STUDIO_PYTHON 或 "python"
    pythonPath: 'C:\Users\you\code\handoff-engine\.venv-win\Scripts\python.exe'
    # 私有交接仓根目录；默认 HANDOFF_ROOT
    engineRoot: 'C:\Users\you\code\handoff-store'
    # 执行型 Agent 绑定的 preset；默认 standard（有 shell / 写文件能力）
    agentPreset: standard
    # 引擎包所在目录；默认本插件包内的 engine/
    engineModulePath: ''
    # 单轮 Agent 的最长执行时间
    runTimeoutMs: 3600000
    # 关掉面板代理（只用 agent 执行能力时）
    panelProxy: true
```

### 为什么 `agentPreset` 不能省

Agent 的**执行能力来自它的 preset 组成**。新建 Agent 如果不显式绑定 preset，
它会加入**当前会话**的组成——从分析型会话里发起一次实现任务，Agent 就没有
shell、没有写文件工具，跑一圈回来只能说「我没有工具可用」。

所以插件在投递 prompt **之前**先绑定 `agentPreset`，绑不上就直接失败并说明原因，
而不是让 Agent 白跑一轮。可用值用插件路由 `/handoff-studio/presets` 查：

```powershell
$token = (Get-Content "$env:USERPROFILE\.dsh\handoff-studio.token" -Raw).Trim()
Invoke-RestMethod "http://127.0.0.1:<DSH端口>/handoff-studio/presets" -Headers @{ Authorization = "Bearer $token" }
```

本机实测：`standard` / `ptc` / `minimal` / `cordis` 都在，`standard` 具备完整
shell 与文件写入能力。

## 安装

```powershell
dsh plugin --profile desktop add C:\Users\you\code\handoff-engine\dsh-handoff-studio
```

装完重启 DSH：`dsh.client` 清单在入口 boot 时才读。

## 开发

```powershell
# 宿主半（假 Cordis 上下文，不起 DSH）
Set-Location C:\Users\you\code\handoff-engine\dsh-handoff-studio
node --test test/host.test.js

# 引擎侧（handoff-engine 已安装进当前 Python）
python -m pytest test -q

# 手工起引擎
$env:PYTHONPATH = 'C:\Users\you\code\handoff-engine\dsh-handoff-studio\engine'
python -m handoff_studio --root C:\Users\you\code\handoff-store --once
python -m handoff_studio --root C:\Users\you\code\handoff-store
```

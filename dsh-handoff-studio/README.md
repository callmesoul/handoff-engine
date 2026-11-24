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

## 额度用完自动换模型（后备模型链）

全自动配置里有一列「后备模型（额度用完接着跑）」，可以按优先级排若干个候选。任务执行时
按「主模型 → 候选 1 → 候选 2 …」依次尝试，直到有一个跑通。

**只在换模型有意义时才换**——判定依据是宿主回传的结构化失败码
（`turn/end.reason.error.code`，取值来自 `@deepseek-ai/dsh-llm` 的 `LLM_FAILURE_CODES`），
不是猜文案：

| 会换下一个 | 不会换 |
| --- | --- |
| 额度用尽（`QUOTA` / `ACCOUNT_QUOTA`）、被限流（`RATE_LIMIT`） | 内容被拒（`CONTENT_REFUSED`）、请求非法（`INVALID_REQUEST`） |
| 鉴权/凭据问题（`AUTH` / `ACCOUNT_TOKEN_INVALID` / `INVALID_CREDENTIAL` …） | 任务被取消、执行超时 |
| 模型不可用（`UNKNOWN_MODEL` / `MODEL_UNSUPPORTED` / `NO_ADAPTER` …） | agent 自己报告 blocked（那是有效结果） |
| 服务端故障 / 超时 / 断流（`SERVER` / `TIMEOUT` / `TRANSPORT` / `STREAM_CLOSED`） | 结果不符合 schema |

拿不到稳定码时退回看 HTTP 状态（401/402/403/429）和少数措辞（quota / rate limit / 额度 …），
两者都没有就不换——宁可一次明确失败，也不要把「任务本身有问题」拖成好几次重跑。

换模型时**不接续**上一个模型的会话：那个 DSH 会话绑的就是刚失败的模型/provider，续跑只会
把同样的失败再演一遍。整条链的日志落成同一 attempt 的日志文件，人点「执行日志」看到的是
这一轮从头到尾发生了什么。全部候选都失败时，失败原因会逐个列出（`候选模型全部不可用：…`）。

链存在 `.handoff-run/studio-state.json` 的 `auto.fallback_models` 里。保存时做三件事：
落到目录里的模型才留（不在目录的会标记出来但保留配置，让人自己决定删不删）、只给 model
时按目录补齐 provider、档位按该模型自己声明的集合校验（不支持的清空用默认）。

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
| GET | `/handoff-studio/agent/run/:id?after=N` | 需要令牌；`{ phase, logs[], cursor, text, error, failure }`；`failure` 是本轮的模型级失败（`{code, message, status}`，无则 `null`） |
| POST | `/handoff-studio/agent/run/:id/cancel` | 需要令牌；尽力取消当前这一轮 |
| ANY | `/handoff-studio/panel/*` | 代理到 studio 引擎（`127.0.0.1:8791`），浏览器同源访问 |

引擎（`127.0.0.1:8791`）自己的路由与原面板同名同形，另外多了几条：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/tasks/:id/notify-status` | 该任务完成通知的送达情况（无记录回 `reason: "no_event"`，不是 404） |
| POST | `/api/notify/flush` | 立刻把 outbox 里到期的通知发一轮（面板顶栏「通知」按钮） |
| GET | `/api/state` | 含 `notify_pump`：投递线程是否在跑、pending / stale 条数、最近错误、水位 |
| POST | `/api/studio/select` | 选 provider / model / effort；显式给了不支持的档位会 **拒绝**，不静默改值 |

同一时刻只跑一个 Agent：handoff 的 worker 本来就是单并发，第二个请求回 `409`。

## 完成通知：为什么引擎里自己带一个投递线程

`handoff` 的完成通知是**两段式**的：

```text
store 进 verified / failed
   └─ emit_task_completed() → 只把 task.completed 写进本机 outbox（pending）

handoffctl notifier（独立常驻进程）
   └─ 定时 drain outbox → 签名 → POST 到前端监听端口
```

原面板由 `run-backend.ps1` / systemd 把第二段一起拉起来，所以没人注意到它是另一个
进程。studio 引擎是 DSH 插件按需 spawn 的**单个** Python 进程——第二段没人跑，
于是面板点「恢复推进 / 验收」后任务变成 `verified`，前端会话却永远收不到
`task.completed`。而「重发完成通知」按钮能用（它自己同步发一次），所以现象很容易
被误判成密钥或网段问题。

现在引擎自己带这段常驻逻辑（`engine/handoff_studio/notifier.py`）：

- 后台线程按 `notify_pump.interval_seconds` 扫 outbox，签名投递、指数退避；
- 「恢复推进」「验收」的线程一结束就 `kick()` 一次，不用等下一个 tick；
- **水位（watermark）**：只自动投递「本轮之后」创建的事件，历史积压（本机实测 82 条）
  不会被一次性灌给前端。水位落盘在 `.handoff-run/notify/studio-pump-watermark.json`，
  引擎重启不会漏掉自己刚入队的事件；
- 状态进 `/api/state` 的 `notify_pump`：顶栏显示「通知 pending N」，任务抽屉显示
  这条通知送到哪个地址、送达时间、最近错误。

历史积压要补发时仍然走原面板那条路（`handoffctl notifier --once`）或面板上的
「重发完成通知」——引擎不会替人决定要不要翻旧账。

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

所以插件在 Agent **发布之前**（`createAgent` / `resume` 的 `setup` 里）就把
`agentPreset` 挂上，挂不上就直接失败并说明原因，而不是让 Agent 白跑一轮。

挂载时机不是随便选的：宿主的 `agentPresets.select()` 只接受**还没开始第一轮**的
会话，否则会抛 `agent-preset/locked`（“This session has already started”）。
「继续上次会话」续的必然是一个跑过的会话，所以在创建之后再 `select()` 一定失败——
这正是旧版本续跑直接报「绑定失败：This session has already started」的原因。
改在 `setup` 里 `mount()` 之后，新建与续跑走的是同一条路，也是 DSH 自己
（`dsh-api-session-controller` 的 `composeAgent()`）创建会话时用的那条。

续跑时优先沿用**会话日志里记录的那个组成**（上一轮实际跑的是什么就接着用什么）；
记录的那个已经不在本机花名册里（profile 换过、preset 被删）时，才退回插件配置的
`agentPreset`，并把降级原因写进执行日志。可用值用插件路由 `/handoff-studio/presets` 查：

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

# Handoff Studio

Handoff Studio 在原任务面板之外，把**实现执行器换成了 DSH 自己**：用面板里选定的
模型跑任务实现，而不是拉起 `codex` / `zcode` 子进程。

## 它解决什么

原来任务面板只有两条执行路径，模型来自 `config/*.yaml` 和 `~/.codex/config.toml`。
Handoff Studio 把执行器接到 DSH 的模型目录上：你在面板顶部选 provider / model /
思考程度，任务就用那个模型跑。

执行器换了，**交付流程没换**：认领、隔离 worktree、路径 gate、提交、推送、PR、
部署对齐、验收依旧是 `handoff` 原来那条链路。所以 Studio 跑出来的任务与原面板
跑出来的通过同一套检查。

## 怎么用

1. 左侧边栏点「工作台」，进入任务面板。
2. 顶部选模型（模型清单来自 DSH 当前可路由的模型；选模型后思考档位会跟着换）。
3. 在列表里点一个任务，右侧抽屉显示需求、事件时间线、历史日志。
4. 点「执行」，任务用当前选定的模型开始跑；执行横幅实时显示日志。
5. 执行中可以在横幅下方的输入框里「打断并续跑」；结束后可以「续话」接着派活。

## 与原面板的关系

两套面板读同一个交接仓，可以同时开着，互不影响：

| | 原面板 `handoffctl panel` | Handoff Studio |
| --- | --- | --- |
| 执行器 | `codex` / `zcode` 子进程 | **DSH Agent**（`codex` / `zcode` 仍可选） |
| 模型来源 | `config/*.yaml`、`~/.codex/config.toml` | DSH 宿主实时模型目录 |
| 界面 | 面板自己发的 HTML | DSH 原生 React 面板 |
| 引擎端口 | 8790 | 8791 |

原面板的代码与执行器表都没被修改。

## 模型选的到底是什么

选中的 provider / model / effort 被记录在 `.handoff-run/panel-state.json` 的
`studio` 键里，重启面板后仍在；下一个任务开始执行时使用。每个模型支持的思考程度
不同，选了该模型不支持的档位会被明确拒绝，不会静默改成别的值。

## 需要知道的限制

- **单并发。** 同一时刻只跑一个任务，与原 worker 一致；第二个请求会被拒绝。
- **执行组成固定为 `standard`。** Agent 的实现能力（shell、写文件）来自它绑定的
  preset；不绑定就会继承发起会话的组成，从分析型会话发起的任务会没有工具可用。
  插件在派 prompt 前先绑定 `agentPreset`（默认 `standard`），绑不上直接失败。
- **无人值守审批。** 执行期间 Agent 不会被问话：插件把该会话的审批策略设为
  不询问，没人能回答的提问会等到超时后按未完成处理。需要人工决策的任务不适合
  这样跑。
- **Dsh 执行器健康检查依赖插件。** 插件没启用时，面板仍能看列表、详情、日志，
  但「执行」会报错说明原因。
- **引擎由插件按需拉起。** 面板第一次请求时才启动，插件卸载时收掉；不需要手工
  起 Python 进程。端口已被占用时直接复用已在监听的引擎，不会叠第二个。
- **状态与界面按 DSH 的实际能力收窄。** 核心闭环（列表、详情、执行、实时日志、
  中断/续话）已可用；全自动模式、验收按钮这类原面板的进阶入口没有搬过来。

## 配置

```yaml
- id: handoff-studio
  config:
    enginePort: 8791
    pythonPath: 'C:\Users\you\code\handoff-engine\.venv-win\Scripts\python.exe'
    engineRoot: 'C:\Users\you\code\handoff-store'
    runTimeoutMs: 3600000
```

完整说明见包内 `README.md`。

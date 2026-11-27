---
name: handoff
description: 创建、查询、删除前后端交接任务，或触发交付验收；只通过 handoffctl 操作。用户说「交给后端」「提个后端单」「后端做完了吗」「删除任务」「清空所有任务」时使用。
---

# handoff

把对话里的后端需求变成一张可校验的任务单。**只调 `handoffctl`，不直接改 `tasks/*.json`，不自己跑 git。**

引擎和交接仓是两个独立仓库。开始执行前：

1. 先在**任务工作区所属的环境**解析 `HANDOFF_ROOT`。Windows DSH 打开 WSL 工作区（`\\wsl.localhost\<distro>\...`）时，不要用宿主 PowerShell 里缺失或陈旧的同名变量；调用 `wsl.exe -d <distro> -- bash -lc 'printenv HANDOFF_ROOT'` 读取该发行版的 login-shell 环境，后续 CLI 也在同一发行版内执行。
2. `HANDOFF_ROOT` 必须指向包含 `config/repositories.yaml` 的交接仓；未设置时只可使用同一环境中确实存在的 `$HOME/code/handoff-store`，否则询问用户。禁止回退到历史仓名，也不要把引擎目录当交接仓。
3. 优先调用同一环境 PATH 中已安装的 `handoffctl`；若不存在，再从该环境的 `HANDOFF_ENGINE_ROOT` 指向的引擎虚拟环境调用。不要从交接仓寻找引擎代码。
4. 下文 `$CTL` 表示 `handoffctl --root "$HANDOFF_ROOT"`；原生 Windows PowerShell 工作区等价为 `handoffctl.exe --root $env:HANDOFF_ROOT`。

安装、环境变量和 Windows/WSL 示例见引擎仓的 `doc/DEPLOY-AGENT.md`。

## 会话里的四个动作

| 用户说 | 你做什么 |
|---|---|
| 「这块要后端做」 | 抽需求 → 写草稿 → `$CTL create ...` → 报任务 ID |
| 「后端做完了吗」 | `$CTL show <id>`，只报状态与缺失环节，不猜 |
| 「验收」/「验一下」 | `$CTL deliver <id>`，按结果分支回报 |
| 「修正验收标准后重跑」 | `$CTL amend <id> ...` → `$CTL reverify <id> ...` → `$CTL deliver <id>`；不得直接改任务 JSON |
| 「前端已验收」/前端自己验收通过 | `$CTL accept <id> --reason '前端验收通过'`（仅 `verified` 可转），或让用户点面板「前端已验收」按钮 |
| 「删除任务」/「清空所有任务」 | 先解析精确范围，再调用 `$CTL delete ... --yes --json` |

不需要用户自己敲命令。所有操作都由 CLI 完成并留在 Git 历史中；创建和状态变化还会生成审计事件。

## 先判断

需求不清楚就先问一句，不要猜。以下任一缺失就停下来问：

- 哪个接口、什么方法、请求/响应长什么样
- 已知的边界情况（空值、历史数据、权限）
- 怎么算做完（能写成结构化检查吗）

### 创建前的验收环境预检

在写草稿和调用 `$CTL create` 之前，先从需求、契约及 `acceptance.checks` 用到的已登记脚本中盘点验收依赖的环境变量和凭据，例如 `SAAS_TEST_SESSION_COOKIE`：

- 发起端/前端：先查 `config/local.yaml` 的 `secrets`（含 `refs`/`bindings` 与 `*_FILE`）和进程环境，已经配置且文件存在/可解析的 secret（如 `OPENAPI_API_KEY`、`DOCS_API_KEY`、`SAAS_TEST_SESSION_COOKIE`）直接取用，不再询问用户。只有确实缺失本机配置或文件时，才停下来明确指出变量名并向用户索取；拿到后由助手写入前端本机 secret 文件与 `local.yaml` 的 `refs`/`bindings`，后续 create 直接复用。检查时只报告“存在/缺失”，不得输出明文值。
- 后端：不要从本机环境推断远端状态，也不要在 create 前追问后端是否手工配置。后端侧以 `$CTL create` 的 `secret_provision` + `secret_preflight` 实测结果为准：前者在已签名通道内把前端 secret 加密推送给后端 listener 自动落盘，后者验证双方均存在且按 `same_value` 要求一致。
- `secret_provision` / `secret_preflight` 任一步 fail closed（本机缺失、后端未开启 provision、ref 未授权、值不一致、协议/签名错误）就不写任务，绝不先创建再等后端索取凭据。
- 凭据值不得写入草稿、`requirement`、契约、附件或回报；用户提供后只能通过双方约定的安全配置方式注入本机运行环境，明文绝不进任务/Git/日志。

## 抽四样东西

1. **requirement**：字段级。接口、方法、请求、响应、边界。不是复述用户的话；必须自包含，不能要求后端工作目录存在前端文档。
2. **acceptance.checks**：只能从这四类里选，写不出就当需求没想清楚：
   - `http`：`method` / `url`（`{base_url}` 开头）/ `expect_status` / `expect_jsonpath`
   - `openapi_diff`：`expected_path` + `actual_url`
   - `repo_script`：`repo` + `name`（必须已在 `config/checks.yaml` 登记）
   - `test_suite`：`repo` + `name`（同上）

   不要写 shell 字符串，schema 会拒绝。
3. **scope**：`allowed_paths` 写清允许改的 glob；`forbidden_paths` 至少包含 `.github/**` 与 `infra/prod/**`；`out_of_scope` 写语义边界（只给人看）。
4. **migration**：需要迁移就写 `plan` / `rollback` / `order`；不确定时写 `required: true` 并说明，别默认 false。

`source.commit_sha` 不用自己查——`--source-path` 会让 CLI 读前端当前 HEAD。`target.base_sha` 也不用写；前端发单不访问后端仓，后端 worker 认领时会固定完整 SHA。

### requirement 与契约文件

`--contract` 会把原文件复制到交接仓的 `contracts/<task-id>/`，worker 还会把契约原文直接嵌入后端执行提示中的“接口契约（原文，只读）”。因此：

- `requirement` 即使脱离源文档也必须足以执行；契约文件用于补充细节和追溯，不能成为启动任务的前置文件。
- 禁止写“以 `doc/foo.md` 为准”“先读取 `doc/foo.md`”或其他会让后端去自己仓库查找前端相对路径的句子。
- 引用文件时优先使用 Markdown 链接，不写裸路径。任务 ID 已知时使用：`[doc/foo.md](/contracts/<task-id>/foo.md)`。
- 草稿阶段若任务 ID 尚未生成，不伪造 `/contracts/` 路径；改写为“详细契约已通过 `--contract` 附带并会内嵌到执行提示”，同时把所有不可缺少的要求写进 `requirement`。
- `create` 成功后，向用户报告实际契约位置时使用返回的任务 ID，例如：`[doc/foo.md](/contracts/ho-20261109-01M6Z245S3KR6KF5B0P3HS0P9A/foo.md)`。

错误示例：

```markdown
以随任务附带的 doc/MOBILE_API_BACKEND_DELIVERY.md 为准；找不到时停止。
```

正确示例：

```markdown
本 requirement 已包含可独立执行的范围与约束。详细契约已通过 `--contract` 附带并会内嵌到执行提示；不得因后端工作目录中不存在前端文档而停止。
```

## 怎么写出去

1. 把草稿写到临时文件（格式参照 `doc/task-draft.example.json`）。
2. 调 CLI：

```bash
$CTL create \
  --from-file /tmp/handoff-draft.json \
  --source-path <前端仓库路径> \
  --contract <契约文件> --contract-kind openapi \
  --attachment <截图或日志>
```

3. 只报告 CLI 的输出：任务 ID、store commit、通知状态、下一步命令。

`create` 必须登记原前端会话：Codex 会话自动使用 `CODEX_THREAD_ID`，DSH 会话使用 `DSH_SESSION_ID`（`session-<uuid>`），并按 id 形状推断执行器类型，`--source-path` 作为续接工作区。普通终端没有上述环境变量时必须显式传 `--source-thread`（必要时再传 `--source-executor`）。不要把会话 id 写进草稿或任务正文。缺少会话或登记失败时任务不会写入 Git；成功创建时 `frontend_session.status` 必须是 `registered`。`--no-session-register` 已禁用，不得使用。

`notification.status` 要按原义回报：

- `sent`：任务已进交接仓，后端监听器也已持久化接收。
- `pending`：任务已经创建成功，但局域网通知暂未送达；outbox 会重试。不能把它说成发单失败，也不能说后端已经收到。
- `disabled`：未启用主动通知，任务仍已进交接仓，后端靠轮询发现。
- `error`：任务已创建，但通知配置错误且没有可靠入队；贴出 `notification.error`。

多项目时不要手抄 `source.repo`：`--source-path` 会从该仓库的 `origin` 识别。目标后端不是默认那个就加 `--target`，环境不同就加 `--environment`。识别失败（本地 remote）时 CLI 会报错，再用 `--source-repo`。

## 删除任务

删除是永久移除当前 Git 快照中的任务单、事件、契约和附件；提交历史仍可恢复。只有用户明确说“删除”时才执行，“取消任务”只做状态转换，不能当成删除。

1. 先用 `$CTL list --json` 核对当前任务和范围。用户指定 ID 时只能删除这些 ID；用户明确说“所有任务”时可直接使用 `--all`，不必重复确认。
2. 执行其一：

```bash
$CTL delete <id> [<id> ...] --yes --json
$CTL delete --all --yes --json
```

3. 按原样报告 `count`、`deleted`、`store_commit`、`notifications_deleted`、`frontend_sessions_deleted`。`count: 0` 表示当时已经没有任务，不得说成失败。

CLI 会同步远端、提交并推送删除，同时清理本机这些任务的租约、待发送通知和已有管理索引。它不会删除业务仓分支、PR 或 Codex worktree。若用户要恢复，说明可对 `store_commit` 做 Git revert；本地通知队列记录不会随 Git 恢复。

缺少 `--yes`、任务 ID 不存在、范围冲突或 push 失败时，把 CLI 错误原文交给用户，不要绕过 CLI 直接删目录或改 SQLite。

## 后端跑完之后

- 配置了双向通知时，任务进入 `verified` 或 `failed` 后，前端 `frontend-listener` 会把消息排回创建任务的原会话。收到该消息后必须先 `$CTL show <id> --json` 拉取并核对通知里的 revision/store commit；`verified` 才转入当前前端仓的契约对齐和 dev 联调，`failed` 则报告 failure/checks，不得直接开始联调。前端验收通过后执行 `$CTL accept <id> --reason '前端验收通过'`，把任务标记为 `frontend_accepted`（前端已验收）终态。
- 没有自动登记或要纠正映射时，用 `$CTL register-session <id> --thread <thread-id> --workspace <前端仓>`；DSH 会话再加 `--executor dsh`（通常能从 `session-` 前缀自动推断）；不要直接改 SQLite。
- 想知道后端 Codex 会话在哪：`$CTL session <id>`，里面有 thread id、worktree、`codex resume` 命令。
- 想把一句话推回某个还在运行的 Codex 会话：`$CTL notify --thread <id> --message "..."`（需要同一台机器的 app-server）。`notify` 只支持 Codex；DSH 会话的回推走 `dsh-handoff-bridge` 插件，由 `frontend-listener` 自动完成，没有手敲命令。桥接地址由插件自己公布到 `$DSH_HOME/handoff-bridge.json`，**不要手写 `notifications.dsh_bridge_url`**——DSH 端口每次重启都变，写死会失效。回推不工作时先跑 `$CTL doctor --role frontend`，看 `dsh:bridge` / `dsh:bridge-token` / `dsh:bridge-live` 三条。

验收标准本身写错时，只允许对 `failure.stage=verification` 的任务使用 `amend`。修订文件必须给出完整 `acceptance`，requirement 文案变化用精确的 `requirement_replacements`，附带契约变化用 `--contract`；随后调用 `reverify`，它保留 merge SHA 和 attempt，不会重新运行 Codex 实现。

## 不要

- 不要塞整段会话。只要契约和事实。
- 不要动业务仓库，不要 `git add -A` / commit / push。
- 不要在 CLI 报校验错时手改任务单去绕过。把错误原文贴给用户。
- 不要伪造 `base_sha`、附件摘要或验收命令。
- 不要把“取消”理解成“删除”，也不要自行扩大到 `--all`。
- 不要用 `/prompts:handoff` 这类旧入口；本 skill 就是入口。

# Handoff Engine：问题审查与实施方案

> 审查范围：`doc/HANDOFF-SPEC.md`、`doc/IMPLEMENTATION.md`、`doc/handoff.md`、`doc/deliver.md`  
> 目标：保留“任务单经 Git 交接、Codex 在隔离环境实现、PR 受控合并、按部署版本验收”的核心思路，同时修正当前文档中会导致实现失败或安全事故的问题。

## 一、结论

方案可以落地，但当前四份文档还不能直接作为实现依据。必须先统一任务目录、状态机、提交 SHA 的含义和 Codex 调用方式，并补上任务 schema、并发控制、隔离工作区、命令执行安全、失败恢复及部署锁。

推荐把系统拆成三个明确边界：

1. `handoffctl`：创建、校验、查询和更新任务单，不让 prompt 直接拼 Git 命令。
2. `handoff-worker`：认领任务、创建隔离 worktree、调用 `codex exec`、验证改动、推送分支和创建 PR。
3. CI/CD：负责测试、合并 gate、部署锁和版本标识；worker 不直接部署，也不直接写 `dev`/`main`。

Codex 是实现执行器，不是状态机、凭据管理器或部署控制器。Git、PR 平台和 CI/CD 分别承担审计、合并和发布职责。

---

## 二、现有文档中的问题

### P0：不修就会阻断主流程

#### 1. 任务目录不一致

- `HANDOFF-SPEC.md` 使用 `handoffs/<id>.json`。
- `IMPLEMENTATION.md`、`handoff.md`、`deliver.md` 使用 `tasks/<id>.json`。

统一为 `tasks/<id>.json`。契约和附件放到 `tasks/<id>/` 会更容易管理；如果坚持单文件任务单，则统一把契约放到顶层 `contracts/<id>/`。

#### 2. `base_commit` 指向了错误的仓库

文档要求前端把“前端仓库当前 HEAD”写入 `base_commit`，随后又要求后端仓库基于它创建分支。两个仓库的 SHA 不相通，后端仓库通常找不到这个 commit。

必须拆成：

- `source.repo`、`source.commit_sha`：记录前端发起时的上下文版本，仅用于追溯。
- `target.repo`、`target.base_ref`：前端声明目标；`target.base_sha` 由后端 worker 认领时固定，作为真正创建分支的基线。

`open` 任务允许暂时没有 `target.base_sha`，使前端无需后端仓权限。后端 worker 认领时必须在远端解析 `base_ref`，并在同一次认领写入中记录完整 `base_sha`；进入 `claimed` 后不得缺失。

#### 3. `result.commit_sha` 无法同时表示分支提交和合并提交

worker 在功能分支上提交后记录的是 head SHA；使用 squash merge 后，`dev` 上会生成另一个 merge SHA。当前文档却要求 `/version` 等于 `result.commit_sha`，因此 squash merge 场景下会一直等待到超时。

必须拆成：

- `result.head_sha`：PR 功能分支最后一个提交。
- `result.merge_sha`：PR 实际合入目标分支的提交，由 PR 平台返回。
- `deployment.observed_sha`：版本接口实际返回的部署提交。

部署和验收使用完整的 `merge_sha`，不是 `head_sha`，也不只比较前七位。环境 SHA 可以更新，但必须通过 Git 提交图证明 `merge_sha` 是它的祖先。

#### 4. 状态机有三套定义

- `HANDOFF-SPEC.md`：`open → ack → in_progress → merged → verified`。
- `IMPLEMENTATION.md`：增加 `delivered`、`merging`、`awaiting_merge`、`merge_failed`。
- 执行章节又说 Codex 跑完后直接写 `merged` 或 `failed`。

建议采用一套最小状态机：

```text
open → claimed → in_progress → delivered → awaiting_merge → merged
                                                        ↓
                                                     deployed
                                                        ↓
                                                    verifying → verified

任意非终态 → failed / cancelled
failed → claimed（attempt + 1）
```

其中：

- `delivered` 只表示分支已推送且 PR 已创建。
- `merged` 只表示 PR 平台确认已经合入，并已得到 `merge_sha`。
- `deployed` 只表示环境版本已经对齐。
- `verified` 只表示所有验收检查通过。

#### 5. prompt 引用的任务单定义并不存在

`handoff.md` 要求“字段严格按 `IMPLEMENTATION.md` 里的任务单定义”，但任务单 JSON 示例实际只在 `HANDOFF-SPEC.md`。不能让 agent 在两份自然语言文档之间猜 schema。

应新增机器可校验的 `schemas/task.schema.json`，所有生产者和消费者只以 schema 为准；Markdown 只解释字段含义。

#### 6. 当前自定义 prompt 的调用名称不准确

现文档说复制到 `~/.codex/prompts/` 后使用 `/handoff` 和 `/deliver`。当前官方文档说明自定义 prompt 显示为 `/prompts:<name>`，因此对应调用应是 `/prompts:handoff` 和 `/prompts:deliver`。如果改成 Codex skill，则使用 `$handoff`、`$deliver` 或从技能列表选择。

另外，仓库里实际文件位于 `doc/handoff.md`、`doc/deliver.md`，并不存在文档所说的 `prompts/` 目录。

推荐最终将它们改成仓库级 skill，但 skill 只调用 `handoffctl`，不直接操作状态文件和远端 Git。

### P1：会造成错误结果、竞争条件或安全风险

#### 7. 自动化仍可能碰到人的工作区

文档写了“自动化不碰人的工作区”，执行步骤却让 worker 在“后端项目目录”中直接开分支并运行 Codex。只要该目录有人未提交改动，流程就可能失败或误提交。

worker 必须使用专用 clone，并为每次 attempt 创建独立 worktree。禁止把开发者日常 clone 配给 worker。

#### 8. ID 分配存在竞争

“每天扫描目录，从 001 起编号”在两个前端同时发单时会撞号。使用 ULID/UUID，或使用 `ho-YYYYMMDD-HHMMSS-<随机后缀>`。标题和日期可以保留可读性，但不能依赖目录扫描分配唯一序号。

#### 9. Git 任务仓存在多写者冲突

前端、worker、合并器和验收器都会修改同一个 JSON 文件。仅执行 `git pull`/`git push` 不能保证状态不会相互覆盖。

每次状态更新必须：

1. fetch 最新任务仓；
2. 校验当前 `revision` 和允许的状态转换；
3. 原子写临时文件后 rename；
4. commit；
5. push；
6. 非快进时重新读取并做有限次数重试，不能盲目 force push。

认领动作要先提交 `open → claimed`。两个 worker 竞争时，只允许第一个成功 push 的 worker继续执行。

#### 10. 缺少租约和崩溃恢复

worker 在 `in_progress` 时崩溃，任务会永久卡住。任务中应记录：

- `claim.worker_id`
- `claim.claimed_at`
- `claim.lease_expires_at`
- `attempt`
- `heartbeat_at`

只有租约过期后，其他 worker 才能接管，并把 attempt 加一。旧 worker 发现自己不再持有租约时必须停止写入。

#### 11. `out_of_scope` 不能直接充当路径 gate

“不做某功能”是语义范围，不一定是文件路径。当前“改动路径都在 `out_of_scope` 之外”的规则无法可靠自动判断。

拆成：

- `scope.out_of_scope`：给人和 agent 看的语义边界。
- `scope.allowed_paths`：自动化允许修改的 glob。
- `scope.forbidden_paths`：绝对禁止修改的 glob。

自动 gate 只对后两项做确定性判断。

#### 12. `acceptance.checks` 是远程代码执行入口

任务单里的任意 shell 字符串会在前端或验收机执行。即使交接仓是私有仓，也不能把“能 push”等同于“可以执行任意命令”。任务内容、日志和契约还可能对 Codex 构成 prompt injection。

优先使用结构化检查类型：

- `http`：方法、URL、请求体、期望状态和 JSONPath 断言；
- `openapi_diff`：两个明确的契约来源；
- `repo_script`：只允许调用业务仓中已提交、已审核的脚本；
- `test_suite`：调用预先注册的测试套件名称。

若必须支持 shell：在无凭据容器中运行，设置命令白名单、工作目录、超时、输出上限和网络策略；禁止把字符串经 `eval` 或 `sh -c` 直接执行。

#### 13. 契约和截图路径可能跨机器失效

`contract.openapi` 和 `repro.screenshots` 如果只是发起机器的本地路径，后端读不到。每个附件必须满足以下任一条件：

- 复制进交接仓并记录相对路径和 SHA-256；
- 指向双方都有权限的制品 URL，并固定版本/摘要；
- 指向某仓库的 `repo + commit_sha + path`。

#### 14. `gh pr merge --auto` 成功不等于已经合并

该命令可能只是开启 auto-merge 或进入 merge queue。worker 必须轮询 PR 状态，直到平台明确返回 `MERGED`，再读取完整的 merge commit OID。超时仍处于 open/queued 时不能写 `merged`。

#### 15. 执行队列串行不等于部署串行

其他人或其他 worker 仍可触发部署。部署互斥必须在 CI/CD 平台按环境实现，例如 GitHub Actions 使用同一 `concurrency.group` 且不取消正在运行的部署。worker 的本地队列不能替代环境锁。

#### 16. 共享 dev 环境会发生版本前移

任务 A 合并并等待验收时，任务 B 可能已部署。若 Git 提交图证明 B 的部署提交包含 A，则 A 仍可验收；若提交已分叉或无法证明，推荐使用独立 preview 环境。

如果只能使用共享 dev：

- 精确相等时可执行验收；
- dev 已前移时，只能确认 A 的 merge SHA 是当前部署 SHA 的祖先，并把结果标记为“非固定版本验收”；
- 对高风险任务应停止自动验收，改用 preview 环境或重新部署目标 SHA。

#### 17. `deploy_sha = "timeout"` 混淆了数据类型

SHA 字段不能存状态文本。使用：

```json
{
  "deployment": {
    "status": "pending|deployed|failed|timeout",
    "target_sha": "<full sha>",
    "observed_sha": "<full sha or null>",
    "environment": "dev",
    "url": "...",
    "updated_at": "..."
  }
}
```

### P2：表达或可维护性问题

- “跨机器 resume 必崩”表述过于绝对。正确说法应是：不要把本地 rollout 文件当作稳定、可移植的交接协议。
- `git status --porcelain` 为空不能单独证明 Codex 没有工作；Codex 可能自行提交。应比较 `base_sha..HEAD` 和工作区 diff，并明确规定由 worker 统一提交。
- “分支已存在就重建”与“失败后保留分支”冲突。应使用唯一 attempt 分支，禁止覆盖已推送分支。
- `--force-with-lease` 不应作为普通重试策略。任务分支出现非快进通常表示存在并发写者，应停止并报警。
- `result.notes` 直接保存 stderr 可能泄漏 token、URL 参数和业务数据，写任务单前要做脱敏和长度限制。
- `$BASE` 与 `{{base_url}}` 两套变量模板不一致。结构化检查后无需模板；保留模板时也必须只选一种。
- 数据库迁移不能只用一个字符串表示。至少要记录迁移脚本、兼容性、执行顺序、回滚方式和是否需要人工 gate。

---

## 三、建议的仓库结构

交接仓建议使用：

```text
.
├── README.md
├── schemas/
│   ├── task.schema.json
│   ├── event.schema.json
│   └── codex-result.schema.json
├── tasks/
│   └── <id>.json
├── events/
│   └── <id>/
│       └── <event-ulid>.json
├── contracts/
│   └── <id>/
│       └── openapi.yaml
├── artifacts/
│   └── <id>/
│       └── ...
├── config/
│   ├── repositories.yaml
│   ├── environments.yaml
│   └── checks.yaml
├── src/
│   ├── cli/
│   ├── worker/
│   ├── task-store/
│   ├── git-host/
│   ├── verifier/
│   └── admin/
├── tests/
└── .agents/skills/
    ├── handoff/SKILL.md
    └── deliver/SKILL.md
```

业务仓不存任务状态，只保留业务代码、测试、契约脚本和 CI 配置。

---

## 四、任务单最小字段

先写 JSON Schema，再写业务逻辑。建议至少包含：

```json
{
  "schema_version": 1,
  "id": "ho-20261105-01J...",
  "revision": 1,
  "attempt": 0,
  "title": "为订单接口增加状态字段",
  "source": {
    "role": "frontend",
    "repo": "org/web",
    "commit_sha": "<full sha>"
  },
  "target": {
    "role": "backend",
    "repo": "org/api",
    "base_ref": "dev"
  },
  "requirement": "...",
  "contract": {
    "kind": "openapi",
    "path": "contracts/<id>/openapi.yaml",
    "sha256": "..."
  },
  "scope": {
    "out_of_scope": [],
    "allowed_paths": ["src/orders/**", "tests/orders/**"],
    "forbidden_paths": [".github/**", "infra/prod/**"]
  },
  "acceptance": {
    "environment": "preview",
    "checks": []
  },
  "migration": {
    "required": false,
    "backward_compatible": true,
    "plan": "",
    "rollback": "",
    "manual_approval_required": false
  },
  "status": "open",
  "claim": null,
  "result": {
    "branch": null,
    "head_sha": null,
    "pr_url": null,
    "merge_sha": null,
    "codex_run_id": null,
    "checks": []
  },
  "deployment": {
    "status": "pending",
    "target_sha": null,
    "observed_sha": null,
    "environment": null,
    "url": null
  },
  "failure": null,
  "created_at": "2026-11-05T00:00:00Z",
  "updated_at": "2026-11-05T00:00:00Z"
}
```

这是 `open` 阶段的最小快照；认领成功后，worker 会补上完整 `target.base_sha`。

字段约束：

- 所有 Git SHA 使用完整值，不截断。
- 时间统一 RFC 3339 UTC；展示时再转本地时区。
- `revision` 每次更新加一。
- `status`、`failure.stage`、检查类型都使用 schema enum。
- `additionalProperties: false`，避免拼错字段后静默运行。
- `requirement`、日志和 notes 设置长度上限。

---

## 五、详细实施步骤

### 阶段 0：先定四个不可后补的决策

1. 确定 Git 托管平台及 API：GitHub 或 GitLab，不在核心逻辑里混写两套命令。
2. 确定验收环境：优先 preview；若只有共享 dev，明确接受“环境可能前移”的限制。
3. 确定 worker 身份：独立 bot 账号、最小仓库权限、独立 Codex 凭据。
4. 确定自动合并边界：数据库迁移、鉴权、支付、生产配置、CI 文件默认要求人工批准。

完成标准：形成一页 ADR，后续代码和 schema 不再猜这四项。

### 阶段 1：建立 schema 和状态机

1. 创建 `schemas/task.schema.json`。
2. 为每个状态转换建立白名单，例如 `open -> claimed` 合法，`open -> merged` 非法。
3. 实现 `validate-task` 命令，在本地和 CI 中校验所有 `tasks/*.json`。
4. 实现字段级校验：仓库是否已注册、`claimed` 及后续状态是否已有完整 base SHA、附件摘要是否正确、路径 glob 是否有效。
5. 给 schema 加版本迁移入口；读取未知高版本时拒绝处理。

测试至少覆盖：缺字段、未知字段、非法状态跳转、短 SHA、无效时间、错误仓库、错误附件摘要。

完成标准：无效任务不能提交到交接仓主分支。

### 阶段 2：实现任务存储层

1. 封装 `TaskStore.get/create/transition/heartbeat/fail`，业务代码不得直接改 JSON。
2. `create` 使用 ULID/随机后缀生成 ID，不扫描序号。
3. `transition` 接收 `expected_revision`，实现 compare-and-set。
4. 更新前 fetch，更新后 commit/push；非快进时重新读取，最多重试三次。
5. 同一任务出现状态冲突时停止自动覆盖，输出双方 revision 和 commit。
6. 所有日志先脱敏再写入任务单；完整执行日志放权限受控的日志系统或 CI artifact。
7. 每次状态变化和关键动作在同一个 Git commit 中追加一个不可变事件文件；已存在的事件禁止修改或删除。

完成标准：两个进程同时认领同一任务，只有一个成功；另一个安全退出。任一任务都能通过事件文件还原完整时间线。

### 阶段 3：实现 `handoffctl create`

1. 从当前前端仓读取 repo 标识和完整 HEAD，写入 `source`。
2. 从已注册配置中选择后端 repo、目标分支；前端只记录 `base_ref`，不读取后端仓。
3. 收集 requirement、契约、范围和验收检查；信息不足时拒绝创建，而不是让 agent 猜。
4. 复制附件到交接仓或写入固定版本引用，并计算 SHA-256。
5. 本地 schema 校验通过后创建任务、commit、push。
6. 输出任务 ID、任务仓 commit 和后续查询命令。

Codex skill 只负责把对话整理成结构化输入，然后调用该 CLI。CLI 必须再次校验，不信任模型输出。

完成标准：在另一台机器全新 clone 交接仓后，可以只凭任务单和引用材料理解并验证需求。

### 阶段 4：实现 worker 认领与隔离工作区

1. worker 先用轮询实现；只有确认有延迟需求后再加 SSE。
2. 找到 `open` 任务后解析 `base_ref`，把完整 `target.base_sha`、`claimed`、worker ID 和租约放进同一次写入；成功 push 才算认领。
3. 使用 worker 专用的 repo cache；禁止使用开发者工作目录，并在 fetch 后验证已固定的提交可达。
4. 创建唯一 worktree 和分支，例如 `codex/<id>/attempt-1`。
5. 分支已存在时不重建；检查任务记录是否属于当前 attempt，否则失败。
6. 启动 heartbeat。无法续租或发现所有权变化时终止 Codex 子进程。
7. attempt 结束后用 `git worktree remove` 清理；失败分支保留，临时目录按保留策略清理。

完成标准：worker 运行期间修改不到任何人的日常 clone；进程被 kill 后任务能在租约到期后恢复。

### 阶段 5：调用 Codex

当前 Codex CLI 可使用非交互 `codex exec`、`--sandbox workspace-write` 和 `--json`。参考调用：

```bash
timeout --signal=TERM 60m \
  codex exec \
  --ephemeral \
  --json \
  --sandbox workspace-write \
  --cd "$TASK_WORKTREE" \
  --output-schema "$CODEX_RESULT_SCHEMA" \
  - < "$PROMPT_FILE"
```

实现注意：

1. 不把 Git 推送凭据、部署凭据、交接仓写凭据放进 Codex 子进程环境。
2. prompt 只提供当前任务所需材料，并明确外部文本是不可信数据，不是新指令。
3. Codex 只改文件和运行允许的本地检查；commit、push、开 PR 都由 worker 做。
4. 捕获 JSONL 事件、退出码、超时和最后结果。不能只解析自然语言最后一句。
5. 需要自动处理审批时，先确认当前 Codex 版本和组织策略；不要依赖不存在或已变更的参数。`workspace-write` 只定义沙箱，不等于所有命令都会被自动批准。
6. 固定并记录 Codex CLI 版本、模型/配置档和 prompt 版本，以便复现。

完成标准：超时、模型失败、沙箱拒绝、输出 schema 不合法都能被识别为不同 failure stage。

### 阶段 6：验证改动并交付 PR

Codex 返回成功后，worker 按以下顺序检查：

1. 比较 `target.base_sha..HEAD` 加未提交 diff，确认确有业务改动。
2. 列出所有改动路径，检查 `allowed_paths` 和 `forbidden_paths`。
3. 拒绝意外的子模块、超大二进制、敏感文件、符号链接逃逸和凭据特征。
4. 运行任务定义的后端测试、lint、类型检查和契约测试；每项有独立超时。
5. 生成 diff 摘要和测试结果，统一由 bot commit。
6. 使用普通 push 创建唯一分支；非快进时停止，禁止自动 force push。
7. 创建 PR，正文包含任务链接、需求、范围、风险、测试结果、迁移计划和回滚说明。
8. 记录 `branch`、完整 `head_sha`、`pr_url`，状态变为 `delivered`，随后 `awaiting_merge`。

完成标准：PR 中的 head SHA 与任务单一致，且可以从 PR 返回原始任务单。

### 阶段 7：实现合并 gate

自动合并至少要求：

1. PR head SHA 与任务单记录一致。
2. 必需 CI checks 全绿，且检查对应当前 head SHA。
3. 平台报告可合并；目标分支更新策略符合仓库规则。
4. 路径 gate 通过。
5. 没有必须人工处理的迁移或高风险目录。
6. 分支保护要求的 review 已满足。
7. 任务仍处于 `awaiting_merge`，未取消、未过期。

触发 auto-merge 后继续轮询 PR。只有 PR 状态确认为 `MERGED` 后才：

1. 从平台 API 读取完整 `merge_sha`；
2. 更新任务为 `merged`；
3. 设置 `deployment.target_sha = merge_sha`。

完成标准：任务写 `merged` 时，该 commit 已真实存在于目标分支历史中。

### 阶段 8：部署对齐

1. CI/CD 必须接受明确的 commit SHA 作为部署输入。
2. 同一环境使用平台级并发锁，`cancel-in-progress` 设为 false。
3. 构建产物关联源码 SHA，部署阶段不得从移动中的分支重新取代码。
4. 版本接口至少返回：

```json
{
  "sha": "<full merge sha>",
  "build_id": "...",
  "built_at": "...",
  "environment": "dev"
}
```

5. verifier 轮询版本接口，使用退避、总超时和请求超时。
6. 精确匹配后更新为 `deployed`；超时写 `deployment.status=timeout`，保留最后观测 SHA 和错误，不能进入验收。

推荐优先创建任务级 preview URL，并在任务完成后按保留策略销毁。共享 dev 只作为低成本降级方案。

完成标准：能证明验收 URL 当前运行的是哪个完整 SHA。

### 阶段 9：实现 `handoffctl deliver`

1. 拉取并校验任务最新 revision。
2. 非 `deployed` 状态只报告进度，不运行检查。
3. 再次读取版本接口，记录验收开始时的 `observed_sha`。
4. 在隔离 verifier 中逐条执行结构化检查；每项记录开始时间、持续时间、退出码和截断后的脱敏输出。
5. 检查之间可按声明并行，但同一有状态测试组必须串行。
6. 验收结束时再次读取版本接口；若 SHA 变化，结果标记为环境漂移，不自动写 `verified`。
7. 全部通过且版本未漂移时转为 `verified`。
8. 失败时写 `failure.stage=verification`、失败检查 ID 和 observed SHA；不要自动修改后端或前端业务代码。

完成标准：验收报告可以回答“检查了什么、在哪个 URL、对应哪个 SHA、何时检查、输出是什么”。

### 阶段 10：再加通知和 SSE

1. 先依赖 Git 轮询跑通完整闭环。
2. 如果延迟确实影响体验，再增加只读状态服务或 SSE。
3. SSE 只是通知；断线后客户端必须回到 Git 任务仓重新读取真实状态。
4. HTTP 服务不接受直接状态写入，或写入也必须走同一个 `TaskStore` 和权限校验。

完成标准：关闭通知服务后，任务仍可完整创建、执行、合并、部署和验收。

### 阶段 11：实现后台记录与审计

后台的目的不是再造一份任务状态，而是把 Git 中的任务快照、不可变事件和外部平台链接索引成易查询的视图。Git 仍是最终真实来源；后台数据库是可随时重建的查询索引。

#### 11.1 事件模型

每个关键动作都追加 `events/<task-id>/<event-ulid>.json`，至少包括：

```json
{
  "schema_version": 1,
  "event_id": "01J...",
  "task_id": "ho-20261105-01J...",
  "task_revision": 7,
  "attempt": 1,
  "type": "task.status_changed",
  "actor": {
    "kind": "user|worker|system",
    "id": "worker-beijing-01"
  },
  "occurred_at": "2026-11-05T08:00:00Z",
  "correlation_id": "01J...",
  "from_status": "in_progress",
  "to_status": "delivered",
  "summary": "分支已推送并创建 PR",
  "metadata": {
    "head_sha": "<full sha>"
  },
  "links": {
    "pr": "https://...",
    "logs": "https://..."
  }
}
```

要求：

1. 事件 ID 使用单调可排序 ULID；不能仅靠秒级时间戳命名。
2. 事件与对应任务 revision 在同一 Git commit 中提交，避免状态已变但审计事件缺失。
3. 事件只追加，不更新；纠错通过新增 `audit.correction_recorded` 事件完成。
4. `metadata` 也要受 schema 约束，不能成为任意敏感数据容器。
5. actor 必须来自认证上下文，不能信任客户端自己填写的用户 ID。
6. correlation ID 串联一次请求、Codex run、CI、部署和验收过程。

建议的事件类型：

- `task.created`、`task.status_changed`、`task.cancelled`；
- `claim.acquired`、`claim.heartbeat`、`claim.expired`、`claim.released`；
- `codex.started`、`codex.completed`、`codex.failed`、`codex.timed_out`；
- `branch.pushed`、`pr.created`、`pr.checks_updated`、`pr.merged`；
- `deployment.started`、`deployment.completed`、`deployment.failed`；
- `verification.started`、`verification.check_completed`、`verification.completed`；
- `admin.retry_requested`、`admin.cancel_requested`、`admin.lease_released`；
- `audit.correction_recorded`。

heartbeat 频率较高，不建议每次都提交 Git；可以只在运行时存储中更新 heartbeat，在固定间隔、租约异常或状态变化时形成持久事件。

#### 11.2 查询索引

实现一个只读 indexer：

1. 监听或轮询交接仓主分支的新 commit。
2. 按 Git commit 顺序读取变化的 task/event 文件并校验 schema。
3. 将规范化数据写入 SQLite（单机 MVP）或 PostgreSQL（多人/高可用）。
4. 保存 `last_indexed_commit`；重启后从该 commit 继续。
5. 遇到非法事件时隔离该记录并报警，不能让整个索引停止。
6. 提供全量 rebuild：清空索引后从 Git 历史和当前文件重建。
7. 后台页面显示“索引最新 commit”和任务仓 HEAD，二者不一致时提示数据可能延迟。

索引只用于查询，禁止成为 worker 判断状态的依据。worker 始终读取任务仓中的最新任务 revision。

#### 11.3 后台查询 API

建议至少提供：

```text
GET  /api/tasks
GET  /api/tasks/:id
GET  /api/tasks/:id/events
GET  /api/tasks/:id/attempts
GET  /api/tasks/:id/checks
GET  /api/tasks/:id/logs/:log-id
GET  /api/workers
GET  /api/metrics/summary
POST /api/tasks/:id/actions/retry
POST /api/tasks/:id/actions/cancel
POST /api/tasks/:id/actions/reverify
POST /api/tasks/:id/actions/release-lease
```

列表查询支持：任务 ID、标题、状态、发起人、目标仓库、worker、时间范围、失败阶段和是否存在迁移。使用游标分页，不使用不稳定的大偏移分页。

管理操作不能直接更新数据库，而要调用 `TaskStore` 生成合法状态转换和审计事件。每个写操作都要求：

- `expected_revision`；
- 幂等键；
- 操作理由；
- 当前用户身份；
- 权限检查；
- 返回产生的 task revision 和 Git commit。

#### 11.4 后台页面

最小页面包括：

1. **总览**：各状态任务数、今日成功率、平均耗时、失败阶段分布、卡住任务。
2. **任务列表**：筛选、排序、分页和保存筛选条件。
3. **任务详情**：需求、范围、当前状态、版本 SHA、PR、部署、验收结果。
4. **时间线**：按事件顺序展示操作者、状态变化、attempt、耗时和外部链接。
5. **执行记录**：Codex 版本、run ID、开始/结束时间、退出类型和脱敏日志入口。
6. **异常中心**：过期租约、长时间无进展、CI/部署超时、版本漂移、验收失败。
7. **worker 状态**：最后心跳、当前任务、版本、队列长度和连续失败数。

原始 prompt、完整日志、请求响应正文和附件默认折叠并受更高权限控制。页面中不得直接渲染未清洗的 Markdown/HTML，避免存储型 XSS。

#### 11.5 权限和审计

建议定义三类角色：

| 角色 | 权限 |
|---|---|
| Viewer | 查看有权限仓库的任务、时间线和脱敏结果 |
| Operator | Viewer 权限，加重试、重新验收、取消未合并任务、释放确认过期的租约 |
| Admin | Operator 权限，加权限配置、数据保留配置和审计导出 |

权限同时受仓库/团队范围约束。管理员也不能在后台绕过受保护分支、伪造 `verified` 或直接改写历史事件。

所有后台写操作都产生 `admin.*` 事件，并记录用户、理由、IP/客户端标识、旧 revision、新 revision 和结果。日志下载使用短期签名 URL，并记录下载审计。

#### 11.6 数据保留与隐私

- 任务状态、状态事件、PR/部署引用长期保留。
- Codex JSONL、测试输出和请求响应正文按数据分类设置 TTL。
- 删除到期日志时保留 `log.deleted_by_retention` 元数据，说明原日志何时因何策略删除。
- 索引库只保存展示和检索必需字段；大日志放对象存储，不塞入数据库。
- 搜索索引前完成脱敏；不能先存明文、展示时才遮盖。
- 审计导出应带时间范围、导出人、水印或摘要，并设置下载有效期。

#### 11.7 后台完成标准

1. 可以从 Git 全量重建后台索引，重建前后任务数、revision 和事件数一致。
2. 任一任务都能查看从创建到当前状态的完整时间线。
3. retry/cancel/reverify/release-lease 都通过 `TaskStore`，能在 Git 中看到对应状态和审计事件。
4. 普通用户无法查看无权限仓库、原始敏感日志或执行管理操作。
5. 索引延迟、事件校验失败和审计写入失败都有明确告警。
6. 后台不可用时不影响 create、worker、merge、deploy 和 deliver 主链路。

---

## 六、安全与运维注意点

### 权限

- worker bot 只拥有后端仓创建分支/PR的权限，不拥有绕过分支保护权限。
- 合并器和部署器使用不同身份；Codex 子进程不持有二者凭据。
- 交接仓写权限不应自动等价于业务仓写权限。
- 生产环境永不由本流程自动部署，除非另有明确审批和审计设计。

### 沙箱

- `workspace-write` 是最低起点，不是完整隔离。对不可信仓库代码，应再放进容器/VM。
- 限制 CPU、内存、磁盘、进程数和运行时长。
- 默认关闭出站网络；安装依赖使用预热镜像、内部缓存或明确域名白名单。
- 不挂载用户 home、SSH agent、云凭据目录和 Docker socket。

### Prompt injection

- issue 文本、日志、源码注释、OpenAPI description 都按不可信数据处理。
- 系统 prompt 明确禁止其改变权限、范围和交付目标。
- 确定性 gate 必须由代码实现，不能让模型自行声明“路径合规”“测试通过”。

### 数据库迁移

- expand/contract 优先，先兼容旧代码再切流量。
- destructive migration、数据回填和不可逆 DDL 强制人工 gate。
- 记录迁移工具版本、执行者、开始/结束时间和回滚/前滚方案。
- 部署应用前后分别在哪一步执行 migration 必须写进 CI，不由 Codex临时决定。

### 可观测性

每个任务至少记录：

- task ID、attempt、worker ID；
- Codex run/thread 标识和 CLI 版本；
- base/head/merge/deploy SHA；
- 各阶段开始、结束和耗时；
- token/费用（若可用）；
- CI、PR、部署和日志链接；
- failure stage 和脱敏错误码。

告警至少覆盖：租约过期、任务卡状态、连续 Codex 失败、CI 超时、合并排队超时、部署 SHA 不匹配和验收环境漂移。

后台展示以 `events/` 和任务快照为依据，查询索引只是派生数据。任何无法在 Git 任务仓或外部平台中找到来源的后台状态，都不得作为自动合并或验收依据。

### 保留与清理

- 任务单和 Git 历史长期保留。
- 完整 Codex JSONL、测试日志和 preview 环境按数据等级设置 TTL。
- worktree 清理由独立 janitor 做，先确认任务非运行态、没有持有租约，再使用 `git worktree remove`。
- 不对未经解析和校验的路径执行递归删除。

---

## 七、测试计划

### 单元测试

- schema 与状态转换；
- ID 唯一性；
- revision/CAS；
- 路径 glob gate；
- 日志脱敏；
- SHA、时间和附件摘要校验；
- PR/部署响应解析。
- 事件 schema、事件类型与 actor 校验；
- 后台角色和仓库范围授权；
- 游标分页及查询过滤；
- 日志脱敏与 XSS 清洗。

### 集成测试

- 两个 worker 同时认领同一任务；
- worker 在 claimed、Codex 运行中、push 后分别崩溃；
- Codex 无改动、超时、输出非法、测试失败；
- PR CI 红、冲突、排队、被人工关闭、被人工合并；
- squash merge 后正确记录 merge SHA；
- 部署超时、版本接口不可达、部署 SHA 前移；
- 验收中途环境变化；
- 任务仓 push 非快进；
- 恶意 acceptance 命令和 prompt injection 文本。
- 任务更新与事件必须在同一 commit 中成功或共同失败；
- indexer 断点续传、重复消费、非法事件隔离和全量重建；
- 两个管理员同时重试时，revision 和幂等键只允许一个操作生效；
- Viewer 越权访问其他仓库和敏感日志时返回拒绝；
- 后台数据库不可用时主链路仍然可用。

### 端到端测试

准备两个最小测试仓和一个假 dev 服务，完整跑通：

```text
create → claim → codex → validate → PR → merge → deploy → verify
```

端到端成功的唯一标准不是“Codex 说完成”，而是任务最终为 `verified`，且 PR、部署版本和验收报告中的 SHA 可以互相对上。

---

## 八、推荐落地顺序

### 里程碑 1：人工闭环

- schema；
- `handoffctl create/show/transition`；
- 人工运行 Codex；
- 人工 PR、部署和 `deliver`；
- 先验证任务内容是否足够清楚。

### 里程碑 2：自动执行，人工合并

- worker、租约、worktree；
- Codex JSONL 和结构化结果；
- 自动 diff/test/path gate；
- 自动创建 PR；
- 仍由人审核合并。

### 里程碑 3：部署对齐和自动验收

- merge SHA 回填；
- preview/dev 版本接口；
- 环境并发锁；
- verifier 和验收报告。

### 里程碑 4：后台查询与审计

- 不可变事件；
- indexer 和可重建查询库；
- 任务列表、详情、时间线、异常中心；
- Viewer/Operator/Admin 权限；
- 带 revision、理由和审计事件的管理操作。

### 里程碑 5：有限自动合并

- 只对低风险路径和无迁移任务开放；
- 观察失败率和回滚率后逐步扩大；
- SSE、通知和 UI 最后再做。

每个里程碑先用 10～20 个真实但低风险任务试运行，再进入下一阶段。

---

## 九、现有四份文档的处理建议

1. 保留 `HANDOFF-SPEC.md` 作为产品目标，但把字段、状态机和目录引用改为链接到 schema 与本实施文档。
2. 用本文件替代 `IMPLEMENTATION.md` 中冲突的流程说明，或将后者缩减为操作手册。
3. 把 `handoff.md`、`deliver.md` 移到 `prompts/` 并按 `/prompts:*` 调用；更推荐改成 `.agents/skills/*/SKILL.md`。
4. 不让 prompt 直接执行 `git add -A`、commit、push 和任意验收 shell；统一调用受测的 CLI。
5. 在开始写 worker 前，先提交 `task.schema.json`、状态转换表和两条成功/失败样例任务，作为后续实现的固定契约。

---

## 十、Codex 能力核对依据

截至本文审查时，官方文档确认：

- `codex exec` 适用于脚本和 CI 的非交互任务；
- 自动化写文件可使用 `--sandbox workspace-write`；
- `--json` 输出 JSONL 事件，可用于机器解析；
- 自定义 prompt 在斜杠命令中显示为 `/prompts:<name>`；
- 对可复用、可版本控制的工作流，可使用带 `SKILL.md` 的 Codex skill。

参考：

- [Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex Slash commands](https://learn.chatgpt.com/docs/reference/slash-commands)
- [Codex Skills](https://learn.chatgpt.com/docs/build-skills)

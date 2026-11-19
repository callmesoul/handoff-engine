# Handoff Engine 交接协议

## 目的

前端在 codex 会话里，把需要后端实现的部分直接交给后端的 codex，后端做完再把结果送回来。全程不靠人肉转发、不靠截图、不靠复述。

要解决的三件事：

1. 上下文断在人际边界。前端说「接口要个 status 字段」，后端得再问一遍含义。
2. 交付靠嘴。谁改了什么、什么时候能验，全靠问。
3. 验收靠猜。后端说好了，前端不知道验的是哪个版本。

## 不做什么

**不同步 `~/.codex/sessions`。**

Codex 会话是本地文件（`sessions/YYYY/MM/DD/rollout-*.jsonl.zst`，zstd 压缩）。跨机器搬它，cwd、项目路径、模型版本全对不上，`resume` 会崩。所以交接的不是会话，是任务单加代码。

**不自动推主干。** 后端产物只到 `feat/<id>`，合 dev 要过 gate。

**不共享任何凭据。** 每台机器用自己的 codex 登录态。

---

## 功能

### 1. 发起交接

前端在会话里说一句「这块要后端做」，`/handoff` 从当前上下文抽出：

- 需求描述、验收标准、不在范围内的事
- 接口契约（openapi 片段、字段含义、类型定义）
- 复现材料（报错堆栈、请求响应、截图路径）
- 前端侧的 `git diff --stat`（说明自己改到哪了）
- `base_commit`：发起时的 commit，后端必须基于它开分支

产出 `handoffs/<id>.json`，commit 推送到交接仓库。

**不塞整段会话。** 几十万 token，后端读不完还费钱。

### 2. 送达

交接仓库单独一个 clone，只放任务单，永远 clean。跟任何人的工作目录隔离。

如果自动化用独立工作目录，用 `git worktree add --detach`，不重复拉历史。但要注意：

- 同一分支不能同时 checkout 到两个 worktree，必须 `--detach`
- `git stash` 是全局的，别在交接仓库里用
- 不能 `rm -rf`，要 `git worktree remove`，否则留孤儿记录

只走 API 合并的场景，连 worktree 都不需要，`git fetch` 就够。

### 3. 后端执行

watcher 轮询交接仓库，捞出 `assignee=backend` 且 `status=open` 的单，在自己的项目目录跑：

```
codex exec --sandbox workspace-write --cd <后端项目> "读 handoffs/<id>.json，按 requirement 与 acceptance 实现，改动留在 feat/<id>，不要碰主干"
```

跑完回填分支名、commit sha、测试结果。状态转 `merged` 或 `failed`。

`sandbox` 必须开。无人值守，它不会问你。

### 4. 合入与部署

`feat/<id>` → `dev` → 验收 → `main`。

dev 合进去自动触发部署。合并这一步是 gate，不让 codex 直接合。

自动合的条件，三条全满足：

- CI 全绿
- rebase 到最新 dev 无冲突
- 改动路径都在 `out_of_scope` 之外

有一条不满足就留给人。

合并走平台 API，不做本地 `git merge`：

```bash
gh pr merge "$PR" --squash --auto
```

本地一次 merge 都不做，所以任何人工作区的未提交改动都影响不到它。

部署两个硬要求：

- **绑 sha，不绑分支。** 用合并后的 commit sha 触发。绑分支的话，排队期间 dev 又前进了，部出来的是别人的代码。
- **串行加锁。** 同一环境同时只跑一个部署。两个人五分钟内先后合，第二个排队。

dev 环境需要 `/version` 返回当前运行的 commit sha。没有这个，验收就是对着空气打拳。

### 5. 验收

前端 `/deliver <id>`：

1. 轮询 `/version`，等 SHA 等于目标合并提交，或用 Git 提交图证明当前 SHA 已包含目标提交。超时或无法证明算失败。
2. 跑 `acceptance.checks`，逐条打印结果。
3. 失败时把 stderr 原文塞回任务单，并附上当前 dev 的 sha。

最后这句很重要。如果当前 dev sha 跟你验收时记的不一样，八成是别人合进来的东西改了接口行为，不是后端没交付好。

`acceptance` 必须是可执行的，不能是「跑一下看看」：

```json
"acceptance": {
  "base_url": "https://dev-api.xxx.com",
  "checks": [
    "curl -sf $BASE/version | grep <commit-sha>",
    "npx openapi-diff openapi.json $BASE/openapi.json",
    "npx vitest run tests/contract --env.dev"
  ]
}
```

写成命令，`/deliver` 就能自动验，绿了才通知人。

### 6. 并行，不串行等

`contract` 里定死 openapi，前端先用 MSW 或 Prism 起 mock，页面照写。后端交付后把 baseURL 切到 dev，跑一次契约测试。

前端从第一天就不阻塞。

---

## 任务单

```json
{
  "id": "ho-20261030-001",
  "title": "",
  "from": { "role": "frontend", "session": "" },
  "to": { "role": "backend" },
  "repos": { "backend": "org/api", "frontend": "org/web" },
  "base_commit": "",
  "requirement": "",
  "contract": {
    "openapi": "contracts/ho-20261030-001.yaml",
    "endpoints": [
      { "method": "GET", "path": "/api/x", "req": "", "res": "" }
    ]
  },
  "repro": { "error": "", "logs": "", "screenshots": [] },
  "acceptance": { "base_url": "", "checks": [] },
  "out_of_scope": [],
  "status": "open",
  "result": {
    "branch": "",
    "commit_sha": "",
    "pr": "",
    "deploy_sha": "",
    "migration": "",
    "notes": ""
  },
  "created_at": "",
  "updated_at": ""
}
```

`migration` 不是必填项。交接范围只到 develop 分支的代码交付，不承担数据库迁移——建单时可以整体省略这个字段，缺省视为「不需要迁移」。若确有迁移诉求，再写 `required: true` 并交清计划与回滚；这类任务要人工批准，且不会因为迁移声明而阻断自动合并（`deny_auto_merge_if_migration_required` 默认关闭）。

状态机：

```
open → ack → in_progress → merged → verified → frontend_accepted
                              ↓
                            failed → 回到 in_progress
```

`verified` 是后端验收通过；前端收到完成通知、在自己的环境里验收成功后再标记
`frontend_accepted`（前端已验收），作为终态记录。两处入口等价：`handoffctl accept <id>`
或交接面板任务抽屉的「前端已验收」按钮。

---

## 硬约束

| 约束 | 原因 |
|---|---|
| 后端只出 `feat/<id>` | 无人值守的 codex 不知道 dev 上压着谁的活 |
| 合 dev 要过 gate | 同上 |
| 部署绑 sha | 防环境漂移 |
| 部署串行 | 防并发顶掉 |
| 自动化不碰人的工作区 | 未提交改动会毁掉流程 |
| `acceptance` 可执行 | 否则验收靠猜 |
| dev 有 `/version` | 否则验的是哪版都说不清 |

---

## 链路

```
前端会话 ──/handoff──▶ handoffs/<id>.json ──git──▶ 后端 watcher
                                                      │
                                                  codex exec
                                                      │
                                                  feat/<id>
                                                      │
                              gate（CI 绿 + rebase 干净）│
                                                      ▼
                                                    dev ──自动部署──▶ /version
                                                      │
前端会话 ◀──/deliver────────────────────────────────────┘
```

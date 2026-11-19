# ADR-0001：交接系统的四个不可后补决策

状态：已接受（2026-11-05）

后续代码和 schema 不再猜这四项。改动其中任意一项，都要先改这份 ADR。

## 1. Git 托管平台：GitHub

- 只用 GitHub API，通过 `gh` CLI 调用：建 PR、读 checks、开 auto-merge、读 merge commit。
- 核心逻辑里不混写 GitLab 命令。接 GitLab 时新增 `GitHost` 实现，不改 store/worker。
- 依据：`gh` 已在本机登录；`gh pr view --json statusCheckRollup` 能一次拿到 checks 与 head OID。

## 2. 验收环境：优先 preview，接受 dev 的降级

- `config/environments.yaml` 里 `preview.shared=false`，`dev.shared=true`。
- 版本接口签名：`GET <env>/version → {"sha": "<full 40/64 hex>", "build_id", "built_at", "environment"}`。
- `observed_sha == target_sha`，或 Git 提交图证明 `target_sha` 是 `observed_sha` 的祖先时，才允许进入验收；验收前后环境 SHA 必须保持不变，全部检查通过后才写 `verified`。
- 共享 dev 已前移时：报告「环境漂移」，写 `deployment.status=failed`，不写 `verified`，也不猜「可能是别人的改动」。
- 生产环境不进入本流程，不提供 prod 条目。

## 3. worker 身份

- 独立 bot 账号 `handoff-worker`，只拥有后端仓「创建分支、推送分支、创建 PR」权限。
- 没有分支保护绕过权限，没有主分支直推权限，没有部署凭据。
- Codex 子进程不继承任何 Git/部署凭据（`sanitize_env` 剔除 `GITHUB_*` / `*TOKEN*` / `*SECRET*` / `SSH_AUTH_SOCK`）。
- 合并器、部署器使用不同身份；交接仓写权限 ≠ 业务仓写权限。

## 4. 自动合并边界

自动合并默认关闭（`worker --auto-merge` 且 gate 通过才开）。即使开启，以下情况一律留给人：

| 条件 | 处理 |
|---|---|
| `migration.required = true` | 阻断（`deny_auto_merge_if_migration_required`） |
| `migration.manual_approval_required = true` | 阻断 |
| 改动命中 `.github/**`、`infra/prod/**`、`**/migrations/**`、`**/auth/**`、`**/payment/**` | 阻断 |
| PR head 与任务单记录不一致 | 阻断 |
| 必需 CI 未全绿或结果不属于当前 head | 阻断 |
| 平台报告不可合并 | 阻断 |

低风险任务先跑 10–20 个真实单，观察失败率和回滚率，再考虑放宽路径清单。

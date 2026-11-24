# 交接任务 scope / allowed_paths 责任归属与优化建议

> 面向：前端（发单方）+ 后端（执行方）双方
> 背景：任务 `ho-20261120-01M7VYCBFGKDHHKEP80V7GTYVB`（补齐 Ticket Order 转账凭据持久化详情与删除能力）
> 日期：2026-11-20
> 结论先行：
>
> 1. **`allowed_paths` 不能删**，它是路径 gate 的强制输入。
> 2. **它的"内容"应由后端（执行方/仓库拥有者）决定**，前端不该猜后端目录结构。
> 3. **前端应提供的是"有哪些后端服务/配置入口"这类语义信息**，由后端模板翻译成路径。
> 4. 这次失败的根源是：前端用了通用模板里的 `src/**`、`prisma/**`、`database/**`，与这个 Go 后端的真实布局完全不符。

---

## 1. 这次任务为什么失败

后端执行 Agent 在 attempt 4 的正常反馈：

> 已完成凭据元数据校验、可空字段迁移 SQL、待接线 OpenAPI 契约及配套测试。现有路由、控制器、服务、模型和存储代码均不在允许修改的路径内，运行时功能未接入。

核对 `lunaXtech/dropspot-api` 的实际目录结构后，情况属实：

| 任务单原本 `allowed_paths` | 本次真正要改的运行时代码 | 是否在允许范围 |
| --- | --- | --- |
| `src/**`、`test/**`、`tests/**`、`prisma/**`、`database/**`、`migrations/**`、`openapi/**`、`docs/**` | `router/saas.go` | ❌ |
| 同上 | `controller/mobileApp/payment.go` | ❌ |
| 同上 | `service/mobileApp/payment.go` | ❌ |
| 同上 | `service/userUpload/userUpload.go` | ❌ |
| 同上 | `model/payment.go` | ❌ |

Agent 只能在允许路径里生成外围产物（校验包、迁移 SQL、OpenAPI 草稿、测试），无法接线到运行时代码，任务只能判 failed。

**这不是 Agent 的问题，是 scope 内容与后端真实布局不匹配。**

---

## 2. 责任归属：前端不该决定后端目录

### 2.1 架构事实（先把"谁写了什么"说清楚）

- `scope` 是任务单 JSON 的字段，创建任务时由发单草稿写入（CLI 从 `--from-file` 草稿读，面板从请求 payload 读）。
- 执行时它有两个消费者：
  1. 写进 Agent 的 prompt（`codex_runner.py:249-253`），告诉 Agent 能改哪些路径；
  2. 路径 gate 强制校验（`pathgate.py:96-125`，`worker.py:397-400`），改动只要不在 `allowed_paths` 里或命中 `forbidden_paths` 就 `gate.failed`，即使 Agent 无视 prompt 也没用。
- 后端侧已有集中配置 `config/checks.yaml` 的 `gate.high_risk_paths`，列了 `.github/**`、`infra/prod/**`、`**/migrations/**`、`**/auth/**`、`**/payment/**`——这些是**后端仓库的确定性禁区**，与任务内容无关。

### 2.2 为什么"让前端写准后端目录"是错的

- 前端开发者不了解后端是 Go 项目、源码在 `router/`、`controller/`、`service/`、`model/` 顶层，也不会知道 `prisma/` 根本不存在。
- 后端目录会重构（比如新增 `internal/`、拆分 `service/userUpload/`），前端不可能实时跟进。
- 让不了解代码库的一方写路径白名单，本质上就是这次失败的成因。

### 2.3 正确的责任划分

| 事项 | 归属 | 理由 |
| --- | --- | --- |
| 后端仓库的目录布局、哪些路径可改、哪些是禁区 | **后端执行方 / 仓库拥有者** | 只有他们知道真实代码结构和安全边界 |
| 需求语义：要改哪个接口、什么行为、边界条件 | **前端发单方** | 这是产品需求，前端最清楚 |
| 契约文件（OpenAPI 等） | **前端发单方提供期望，后端确认可实现** | 接口 shape 双方约定 |
| `scope.allowed_paths` 的具体 glob | **后端侧生成/填充** | 属于后端仓库知识 |
| `scope.forbidden_paths` 的确定性禁区 | **后端侧配置（`checks.yaml`）为准** | 与任务无关，是仓库级红线 |

**一句话**：前端说"我要什么"，后端说"我能在哪些文件里改"。

---

## 3. 具体优化方案

### 方案 A（推荐）：后端侧提供"scope 模板"，发单时后端自动填充

在交接仓的 `config/` 下维护一份按仓库区分的路径模板，例如 `config/scope-templates.yaml`：

```yaml
repositories:
  lunaXtech/dropspot-api:
    allowed_paths:
      - "router/**"
      - "controller/**"
      - "service/**"
      - "model/**"
      - "internal/**"
      - "pkg/**"
      - "migrations/**"
      - "openapi/**"
      - "tests/**"
      - "docs/**"
    forbidden_paths:
      - ".github/**"
      - "infra/prod/**"
      - "**/auth/**"
```

流程调整：
1. 前端发单草稿里**不写** `allowed_paths`/`forbidden_paths`，只写 `out_of_scope`（语义边界）。
2. `handoffctl create`（或面板创建）根据 `target.repo` 自动从模板填充 scope。
3. 后端有新增模块（如 `service/newFeature/`）时，只改模板，不改每一张任务单。

这样路径知识只维护一份，且永远在懂后端的人手里。

### 方案 B（最小改动）：前端草稿里 `allowed_paths` 留空，后端认领时填充

如果暂时不想改引擎，可以约定：
- 前端草稿写 `"allowed_paths": []`；
- 后端 worker 认领任务时，按 `config/scope-templates.yaml` 填充（或拒绝空 scope 并提示后端补）。

注意：当前 schema 允许空数组，但 `pathgate.evaluate` 会因空 allowed_paths 拒绝放行，所以必须保证填充发生在 gate 之前。

### 方案 C（兜底）：发单接口加"按仓库自动补全"

面板创建任务的入口（`service.py:1407-1411`）目前是：

```python
"allowed_paths": _path_list(payload.get("allowed_paths")) or ["**"]
```

把 `["**"]` 这个危险兜底改成"按 target.repo 查模板填充"，既避免全放行，又避免前端瞎猜。

---

## 4. 关于"allowed_paths 要不要了"

明确回答：**不能去掉。**

- 它是路径 gate 的确定性输入（`pathgate.py:103-104` 明确拒绝空 allowed_paths）。
- 它是 Agent prompt 的一部分（`codex_runner.py:249-253`），没有它 Agent 不知道边界。
- 去掉它等于把整个仓库无差别开放给自动化改动，风险不可控。

正确的做法不是取消，而是**把它从"前端每次手填"改成"后端集中维护、发单时自动注入"**。

---

## 5. 补充：当前的 `forbidden_paths` 已经很合理

这个任务的 `forbidden_paths` 是 `.github/**`、`infra/prod/**`、`**/auth/**`，与后端 `checks.yaml` 的高危路径规则一致（后者还多了 `**/migrations/**`、`**/payment/**`，用于禁止自动合并而非禁止修改）。这部分不需要前端动。

---

## 6. 针对本任务的处置建议

- 任务 `ho-20261120-01M7VYCBFGKDHHKEP80V7GTYVB` 当前是 `open`（revision 27，attempt 4）。后端侧曾为推进任务扩展过 `allowed_paths`（加了 `router/**`、`controller/**`、`service/**`、`model/**` 等），这属于"执行方自填 scope"的临时措施。
- 若采用方案 A/B，应把这次的修正沉淀到 `scope-templates.yaml`，以后同类任务不再重复踩坑。
- 上一轮已生成的辅助产物（`src/paymentproof/`、`migrations/`、`openapi/`、`tests/`、`docs/`）仍在 `D:\project\dropspot-api`，重新发单时请在 requirement 里注明"已完成，请勿重复生成，直接接线"。

---

## 7. 一句话给前端

> `allowed_paths` 不能取消，但它不该由你们猜后端目录。你们只管说清"要改什么接口、什么行为"，路径白名单由后端按仓库模板维护、发单时自动填充。这次失败就是因为草稿里写了 `src/**`、`prisma/**` 这些不存在的路径，漏了 `router/`、`service/`、`model/`。建议按方案 A 做"后端 scope 模板 + 自动注入"。

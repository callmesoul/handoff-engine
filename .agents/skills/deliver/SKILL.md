---
name: deliver
description: 拉取后端交付、等部署对齐、跑结构化验收并回报。当用户说「验收」「跑 deliver」「后端做完了吗」「验一下 ho-xxx」时使用。
---

# deliver

参数是任务 ID。没给就问。

引擎和交接仓是两个独立仓库。开始执行前：

1. 读取环境变量 `HANDOFF_ROOT`，它必须指向包含 `config/repositories.yaml` 的交接仓；未设置时只可使用确实存在的 `$HOME/code/handoff-store`，否则询问用户，不要把引擎目录当交接仓。
2. 优先调用 PATH 中已安装的 `handoffctl`；若不存在，再从 `HANDOFF_ENGINE_ROOT` 指向的引擎虚拟环境调用。不要从交接仓寻找引擎代码。
3. 下文 `$CTL` 表示 `handoffctl --root "$HANDOFF_ROOT"`；Windows PowerShell 等价为 `handoffctl.exe --root $env:HANDOFF_ROOT`。

安装、环境变量和 Windows 示例见引擎仓的 `doc/DEPLOY-AGENT.md`。

## 做一件事

```bash
$CTL deliver <id> --json
```

`--json` 拿结构化结果。这一个命令内部已经做了：

1. 同步任务仓，读最新 revision；
2. 状态不到 `deployed` 时，先轮询版本接口；`observed_sha == merge_sha`，或 Git 提交图证明目标是当前 dev 的祖先，均算已部署；
3. 部署超时写 `deployment.status=timeout`，**报「部署没完成」，不是「验收通过」**；
4. 环境 SHA 对不上就报环境漂移，不跑检查、不写 `verified`；
5. 跑 `acceptance.checks`，逐条记状态、耗时、脱敏输出；
6. 验收前后各读一次版本接口；即使两版都包含目标，验收过程中 SHA 发生变化仍判环境漂移，结果不采信；
7. 全绿且未漂移才写 `verified`。

## 怎么回报

按结果分支，原文优先，不要总结成「大概没问题」：

- `verified`：列出每项检查的 PASS，附 `observed_sha`。然后按 `contract` 对齐前端调用代码。前端在自己环境验收通过后，运行 `handoffctl accept <id> --reason '前端验收通过'`（或让用户点交接面板的「前端已验收」按钮）把任务标记为 `frontend_accepted` 终态。
- `failed`：贴失败检查的 `id` 与输出原文，附 `observed_sha`。
- `deployment_not_ready`：说明目标 SHA 和当前观测 SHA，让用户决定等还是重部。
- `environment_drift`：明确写「验收时的环境和现在不是同一版」，附两个 SHA。
- `not_ready`：只报当前状态，继续等。

## 不要

- 不改后端代码，不改任务单 JSON。
- 若已通过 `handoffctl amend` 修正 verification 契约，先用 `handoffctl reverify` 回到 `deployed`，再运行 `deliver`；不要把契约错误变成新的 Codex attempt。
- 环境 SHA 不一致时，不要「先跑跑看」。
- 不把超时说成通过，不把部分通过说成全绿。

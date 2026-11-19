# dsh-handoff-studio 遗留面板 bug 规避审计

> 审计日期：2026-11-18 · 审计对象：`ds-handoff-studio` 的 engine（Python）与独立前端/宿主层
> 结论：针对旧版 handoff 交接面板修过的全部代表性 bug，Studio 均已规避，**无修复缺口**。
> 规避方式分两类：**继承**（Studio 复用 `D:\handoff\app\src\handoff` 的 store/panel.service/worker/cli，修复自动生效）与**独立实现对齐**（engine 路由、DshAgentRunner、client.js、index.js 自建且已补齐）。

## 规避方式与运行前提

Studio 引擎 `StudioService(PanelService)` 继承 `handoff.panel.service.PanelService`，并从
`handoff.config/store/worker/cli/schema` 复用同一套管线，因此 store 层与 panel.service 层的修复
**直接继承**。引擎在运行时通过 `PYTHONPATH`/解释器加载 `handoff` 包，不打包固定版本。

> ⚠️ **运行前提**：须将 `HANDOFF_STUDIO_PYTHON`/`pythonPath`/`HANDOFF_ROOT` 指向含全部修复的
> `D:\handoff\app\.venv-win\Scripts\python.exe`（其 import handoff 指向 `D:\handoff\app\src\handoff`，
> 当前 `app` HEAD 为 `db60f9c`）。若机器上有多个 handoff 安装或 venv 被降级，继承层修复会静默丢失。

## 逐项对照

| # | 旧面板 bug（对应 commit） | Studio 规避方式与证据 |
|---|---|---|
| 1 | 任务在列表重复：`.tmp-*.json` 残留被 `git add -A` 提交（`7caeb9a`） | **继承**。`app/src/handoff/store.py` `_commit` 追加 `:(exclude)**/.tmp-*`；`_atomic_write` 用 `.tmp-` 前缀 + `finally` 清理；`_scan` 跳过点开头文件。 |
| 2 | 引擎/面板端口被占、撞 frontend-listener 回推端口（8789→8790，`939b1fd`） | **独立对齐更好**。Studio 用 8791；`lib/index.js` `engineAlive` 探活→`reused` 复用→`portTaken` 重启竞态延时重探，根治占用竞态。 |
| 3 | 交付后不自动验收、并发验收撞非法状态跳转（`00a4203`） | **继承**。`PanelService.deliver/advance` 后台跑 `cmd_deliver`，与 worker 自动验收共用 `_verify_mutex` 非阻塞互斥；目录模式推进到 verified；`advance` 拦非法状态。 |
| 4 | 重发完成通知依赖内存 `_commit`，从磁盘读永远假兜底（`ae05e69`） | **继承**。`resend_completed` → `store.task_commit(id)`，从 `origin/<branch>` 优先、`HEAD` 兜底反查该任务单最后落盘 commit，不再走内存回传字段。 |
| 5 | openapi 验收地址 404：裸 `{base_url}/openapi.json`（`9878c43`） | **继承**。`handoff/cli.py:resolve_id_placeholders` 把该精确值规范化为 `/docs/openapi.json?apikey={env:DOCS_API_KEY}`；worker 自愈链自动覆盖。 |
| 6 | 执行器失败原因被吞成结构化解析失败（`e424ed5`） | **独立实现已补**。`DshAgentRunner.run`：通信失败→`通信失败:{exc}`；Agent 侧真实失败→透出 `payload.error`；HTTP 401/403/404/413→`_describe_failure` 给可操作说明；仅当最终文本确无 JSON 才报 `invalid_output` 且带 `log_path`。 |
| 7 | 弹窗/抽屉被压扁、按钮白底白字（Studio 自身 `f43537d`/`1c0ea67`） | **独立实现已补且有回归测试**。`test/client.test.js`：`no control hardcodes a text colour`（`color` 取自主题）、`.hs-mbody must not use a zero flex-basis`。 |
| — | 任务列表分页（`9878c43` 含） | **独立实现**。engine `/api/tasks` 用 `_positive_int(page, page_size, cap=100)`；`list_tasks_page` 越界回退 `clamp(page, 1..pages)`，返回 `total/page/pages` 兼容字段。 |

## 测试状态

- Studio node 测试：21 pass（含端口竞态、UI 样式、token、agent run、config 校验）
- Studio engine python 测试：41 pass（继承层回归 + studio 服务/分页）
- 均于审计当日在本机跑通。

## 未发现缺口

继承层与独立实现层均无遗漏项。当前无需代码改动。
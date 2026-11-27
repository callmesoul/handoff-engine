# dsh-handoff-panel

DeepSeek Harness 的**前端交接面板**插件：在侧边栏增加「前端任务面板」，并在主区嵌入本机的 `handoffctl panel --role frontend`。

本插件只应安装在**前端开发机的 DSH profile**，后端 worker / operator 部署不要安装。它只提供前端职责：

- 查看、筛选和同步交接任务；
- 向 backend 仓库创建任务；
- 查看 PR、部署状态、验收要求和事件时间线；
- 运行部署对齐与结构化验收（deliver）；
- 将 `verified` 任务标记为「前端已验收」。

它不提供后端执行器、手动/自动执行模式、认领/重试/释放租约、会话续跑、日志、合并/部署推进或通知重放。角色边界不仅在页面中隐藏，Python HTTP 服务也会拒绝这些后端 API。

和已有的 `dsh-handoff-board`（「交接板」）是两回事：

| | 交接板 `dsh-handoff-board` | 前端任务面板 `dsh-handoff-panel` |
| --- | --- | --- |
| 数据源 | 插件自己扫会话文件 | 嵌入 `handoffctl panel --role frontend` |
| 功能 | 列出交接单、开 DSH 会话 | 前端发单、跟踪、验收与最终确认 |
| 端口 | DSH 宿主路由 | 面板自身 `127.0.0.1:8790` |

## 结构

```text
dsh-handoff-panel/
├── package.json      # dsh.bundle.patch + dsh.client（platform: web）
├── cordis.patch.yml  # 插入 handoff-panel 宿主 fiber
├── lib/index.js      # 宿主半：/handoff-panel/{health,status,launch}
├── lib/client.js     # 浏览器半：sidebar.panellist + main 成对注册
└── README.md
```

## 宿主半路由

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/HEAD | `/handoff-panel/health` | `{ ok: true }` |
| GET/HEAD | `/handoff-panel/status` | 探测 `/api/state`，并要求返回 `role: frontend` |
| POST | `/handoff-panel/launch` | 分离进程拉起前端面板 |

面板不经过 DSH 反代：浏览器半直接嵌入面板自己的 loopback origin，页面中的 `/api/...` 请求仍与 Python 服务同源。

## 浏览器半

- `sidebar.panellist` 注册 `id: handoff-panel`、`order: 25`、`label: 前端任务面板`；
- `main` 注册同一个 key；
- 状态条只接受 `role: frontend` 的面板，避免误嵌入后端控制台；
- 「重新载入」只刷新 iframe，不会再启动一个面板进程；
- 未启动时可通过宿主路由启动前端角色面板。

## 配置（可选）

在 profile 的 `cordis.patch.yml` 中给插件配置：

```yaml
- id: handoff-panel
  config:
    url: http://127.0.0.1:8790
    # port: 8790
    # 未写 --role 时插件会自动补 frontend；显式写其他角色会拒绝加载
    # startCommand: ~/code/handoff-engine/.venv/bin/handoffctl --root ~/code/handoff-store panel --role frontend --no-open
    # wslDistro: Ubuntu
```

`url` 只接受回环地址。Windows 宿主默认通过 `wsl.exe -d Ubuntu` 启动；Linux/macOS 默认直接通过 `/bin/bash -lc` 启动。

## 安装（仅前端部署）

后端部署跳过本节，不要把本插件加入后端 DSH profile。

pnpm 不能从 UNC 路径安装，Windows DSH + WSL 源码场景先复制到本地磁盘：

```powershell
$Stage = "$env:LOCALAPPDATA\dsh-handoff-panel-stage"
Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
Copy-Item -Recurse -Force `
  '\\wsl.localhost\Ubuntu\home\you\code\handoff-engine\dsh-handoff-panel' `
  $Stage

Set-Location $HOME
& "$env:LOCALAPPDATA\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd" `
  plugin --profile desktop add "file:$Stage"
```

把 `you` 换成 WSL 用户名。安装或升级后重启 DSH，使 Host/Client 清单重新加载。

自定义 `startCommand` 未写角色时，插件会自动补上 `--role frontend`；显式配置其他角色会被拒绝。默认命令已包含该参数。

## 卸载

```powershell
Set-Location $HOME
& "$env:LOCALAPPDATA\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd" `
  plugin --profile desktop remove dsh-handoff-panel
```

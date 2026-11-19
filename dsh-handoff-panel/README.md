# dsh-handoff-panel

DeepSeek Harness 插件：在侧边栏加一行「任务面板」，主区里直接用 iframe 嵌入
本机运行的 Python 任务面板（`handoffctl panel`）。

和已有的 `dsh-handoff-board`（「交接板」）是两回事：

| | 交接板 `dsh-handoff-board` | 任务面板 `dsh-handoff-panel` |
| --- | --- | --- |
| 数据源 | 插件自己扫 `~/.codex` 与会话文件 | 直接内嵌 `handoffctl panel` 的 HTTP 页 |
| 功能 | 列出交接单、开 DSH 会话 | 完整的发单/执行/验收/日志/模式切换 |
| 端口 | 无（挂在 DSH 宿主 `:58995/handoff-board`） | 面板自身 `127.0.0.1:8790` |

## 结构

```
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
| GET/HEAD | `/handoff-panel/status` | `{ ok, url, reachable, status, mode, error }`，1.5 秒探测 `/api/state` |
| POST | `/handoff-panel/launch` | 分离进程拉起面板，返回 `202 { ok, started, command }` |

面板**不经过** DSH 反代：浏览器半直接框面板自己的 origin，这样面板里那 11 处绝对路径
`fetch("/api/...")` 仍然是同源请求，不需要改写前缀。

## 浏览器半

- `sidebar.panellist` 注册 `id: handoff-panel`、`order: 25`、`label: 任务面板`
- `main` 注册同一个 key —— 侧边栏那行必须与 main 成对，缺一个点击就会抛错
- 主面板：顶部状态条（在线/未启动、地址、模式、重新载入/启动按钮）+ 下方 iframe
- 未启动时显示启动指引，并提供「帮我启动」，走宿主半的 `/launch`

## 配置（可选）

在 `~/.dsh/profiles/desktop/cordis.patch.yml` 里给这一行加配置：

```yaml
- id: handoff-panel
  config:
    url: http://127.0.0.1:8790
    # 面板端口变了就同步改这里；给了 port 会自动补到启动命令后面
    # port: 8790
    # startCommand: ~/code/handoff-engine/.venv/bin/handoffctl --root ~/code/handoff-store panel --no-open
    # wslDistro: Ubuntu   # 面板跑在 WSL 里时，用 wsl.exe -d <distro> 拉起
```

`url` 只接受回环地址（`127.0.0.1` / `localhost` / `::1`），其他一律在加载时报错。

`wslDistro` 在 Windows 上**默认就是 `Ubuntu`**（宿主跑在 Windows、面板跑在 WSL，这是本机的常态）；要想让启动命令在本机原生执行，把它显式设成空字符串。Linux/macOS 上默认值为空，直接走 `/bin/bash -lc`。

## 安装

pnpm 不能从 UNC 路径装，先拷到本地磁盘：

```powershell
Copy-Item -Recurse -Force `
  '\\wsl.localhost\Ubuntu\home\you\code\handoff-engine\dsh-handoff-panel' `
  "$env:LOCALAPPDATA\dsh-handoff-panel-stage"
```

把示例中的 `you` 换成 WSL 用户名。然后（务必在 Windows 本地目录执行，UNC 下 `cmd.exe` 会拒绝）：

```powershell
Set-Location $HOME
& "$env:LOCALAPPDATA\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd" `
  plugin --profile desktop add "file:$env:LOCALAPPDATA\dsh-handoff-panel-stage"
```

装完**必须重启应用**：`dsh.client` 清单在入口 boot 时才读。

## 卸载

```powershell
Set-Location $HOME
& "$env:LOCALAPPDATA\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd" `
  plugin --profile desktop remove dsh-handoff-panel
```

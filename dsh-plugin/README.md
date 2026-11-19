# dsh-handoff-bridge

把 handoff 的完成事件排回创建任务的原 **DSH** 会话。

Codex 有 `codex queue --thread`，DSH 没有等价命令行：消息只能由宿主进程内的
`sessionController.prompt()` 入队——那是唯一会做 requestId 幂等、按需 resume
会话、并把消息当作 owner 用户消息投递的入口。所以本插件在 DSH 宿主的
webserver 上开一个 loopback 前缀路由，由 `handoffctl frontend-listener`
用共享令牌 POST 过来，插件再调 `prompt()`。

```text
handoffctl frontend-listener
        │  POST /v1/handoff/dsh/prompt   (Bearer <token>)
        ▼
dsh-handoff-bridge  ──sessionController.prompt()──▶  原 DSH 会话
```

## 装到某个 profile

插件必须装进目标 profile 的 `node_modules`，因为 cordis 的 loader 用锚在
该 profile `package.json` 作用域里的 `import.meta.resolve` 解析入口：

```bash
dsh plugin --profile desktop add /path/to/handoff-engine/dsh-plugin
```

装完 `cordis.patch.yml` 会自动 insert 一个 `dsh-handoff-bridge` 条目
（`dsh.bundle.patch` 干的）。重启 DSH 后插件生效。

也可以直接手写 profile 的 `cordis.patch.yml`：

```yaml
- insert:
    - id: dsh-handoff-bridge
      name: dsh-handoff-bridge
```

## 端口怎么找

DSH 的 webserver 端口是**每次启动时由内核分配**的（默认配置 `port: 0`），
重启就变；写死配置过一天就失效。所以插件在监听真正绑定之后，把实际地址
写进 `$DSH_HOME/handoff-bridge.json`：

```json
{
  "version": 1,
  "name": "dsh-handoff-bridge",
  "url": "http://127.0.0.1:59945/v1/handoff/dsh/prompt",
  "origin": "http://127.0.0.1:59945",
  "path": "/v1/handoff/dsh/prompt",
  "pid": 40164,
  "updatedAt": "2026-11-16T16:23:06.000Z"
}
```

`handoffctl` 按这个顺序找地址：

1. `notifications.dsh_bridge_url`（显式配置，压过一切）；
2. `$DSH_HOME/handoff-bridge.json`（插件自公布，端口变了也跟得上）；
3. `DSH_WEB_URL`（只有从 DSH 会话里启动时才有，且是启动那一刻的端口）。

插件被卸载时会删掉这个文件（只在 `pid` 对得上时删，多个 DSH 实例共用
一个 `$DSH_HOME` 时不会误删别人的）。文件里**不含令牌**。

`handoffctl doctor --role frontend` 会显示地址是从哪条路找来的，并实地打
一次 `/health` 确认那个端口上真有插件在听——只看文件不够：DSH 崩过之后
文件还在，端口早没了。

## 配置

全部可选，默认值就能用：

| 键 | 默认 | 说明 |
|---|---|---|
| `path` | `/v1/handoff/dsh` | 注册的路由前缀，`/prompt` 与 `/health` 挂它下面 |
| `token` | `""` | 共享令牌；留空则读 `tokenFile` |
| `tokenFile` | `$DSH_HOME/handoff-bridge.token` | 令牌文件；不存在时自动生成 64 位十六进制并 `chmod 600` |
| `endpointFile` | `$DSH_HOME/handoff-bridge.json` | 自公布端点的位置；留空用默认 |
| `maxBodyBytes` | `65536` | 超过直接 413 |
| `loopbackOnly` | `true` | 非 loopback 来源 403 |
| `promptTimeoutMs` | `60000` | `prompt()` 的 AbortSignal 超时 |

写在 profile 的 `cordis.patch.yml` 里覆盖：

```yaml
- id: dsh-handoff-bridge
  config:
    maxBodyBytes: 262144
```

## 令牌

插件一加载就生成令牌并原子写入 `$DSH_HOME/handoff-bridge.token`（`0600`），
不用等第一条请求——装完重启 DSH 就能在 `$DSH_HOME` 下看到这个文件。
`handoffctl` 默认读同一个文件，所以两边不需要手工同步；要换位置就在
handoff 侧配 `notifications.dsh_bridge_token_env` 指向一个环境变量。

如果 handoff 跑在 WSL 里而 DSH 是 Windows 桌面应用，两边看到的 `$HOME`
不是同一个目录；`handoffctl` 会自动兜底去找 `$USERPROFILE/.dsh` 和
`/mnt/c/Users/<user>/.dsh` 下的同名文件。

## 接口

`GET <path>/health`

```json
{"ok": true, "name": "dsh-handoff-bridge", "path": "/v1/handoff/dsh/prompt", "port": 59945}
```

免令牌，但会顺手确认令牌文件可读——读不到回 503，免得等到第一条完成事件
才发现问题。`port` 只在监听绑定之后出现。

`POST <path>/prompt`

```json
{"sessionId": "session-…", "requestId": "handoff-<event id>", "mode": "queue", "text": "…"}
```

| 状态 | 含义 |
|---|---|
| 200 | 已入队（`{"accepted": true}`） |
| 400 | envelope 不合法（缺 sessionId/requestId、text 全空白、mode 不是 queue/steer） |
| 401 | 令牌不符 |
| 403 | 非 loopback 来源 |
| 404 | `session/not-found`——会话已删或 id 写错 |
| 405 | 请求没打到插件（路由未注册时落到 SPA fallback）——插件没启用 |
| 409 | `session/agent-busy`——会话正在跑，稍后重试 |
| 413 | body 超限 |
| 500 | 其它宿主错误 |

`requestId` 就是幂等键：`handoffctl` 用事件 id 派生（`handoff-<event id>`），
投递成功但响应丢失时重试不会把同一条完成事件排进会话两次。

## 安全

- 只监听 DSH 自己的 webserver，不额外开端口。
- 默认只接受 loopback（`127.0.0.0/8`、`::1`，含 `::ffff:` 映射形式）。
- 令牌比较用 `timingSafeEqual`，长度不等直接拒。
- 不走 `/api` 的 cookie 认证层——那条路是给浏览器用的，插件路由由自己的令牌守。
- 端点文件是 `0644`，只含地址与 pid，不含令牌。

## 开发

```bash
cd dsh-plugin
node --test test/bridge.test.js
```

测试用 `node:test`，把 `webServer.register`、`webServer.port`、`getOrigin()`
与 `sessionController.prompt` 换成最小假实现后直接打 HTTP，不需要起 DSH。

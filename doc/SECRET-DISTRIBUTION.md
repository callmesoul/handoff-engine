# 双端验收密钥分发

handoff-engine 使用 **公开引用 + 双端本机解析 + 同步预检**，不会把密钥值、文件路径或可复用指纹写入任务单、Git、通知队列或验收结果。

## 配置

前端发起端和后端 verifier 分别在各自未跟踪的 `config/local.yaml` 中配置同一个 public reference；两侧路径可以不同：

```yaml
secrets:
  refs:
    saas-test-session: /run/secrets/saas-test-session-cookie
  # HTTP/OpenAPI 占位使用自定义 ref（或仅来自进程环境的同名 ref）时，
  # 必须在两端明确批准 env → ref。ref 已列在 refs 时，同名 env 可省略 binding。
  bindings:
    SAAS_TEST_SESSION_COOKIE: saas-test-session

notifications:
  # 前端指向后端 listener；预检 URL 默认由它同源派生。
  url: http://192.168.1.20:8788/v1/handoff/wake
  # 只有拓扑不同时才显式配置，路径必须精确匹配。
  # preflight_url: https://handoff.example/v1/handoff/secret-preflight
```

通知共享密钥 `HANDOFF_NOTIFY_SECRET` 仍须在两端配置且至少 32 字节；它只认证预检请求/响应，绝不会注入执行 agent 或作为验收密钥使用。

registered suite/script 在 `config/checks.yaml` 声明它需要的最小集合：

```yaml
test_suites:
  saas-acceptance:
    repo: your-org/api
    argv: ["python", "-m", "pytest", "tests/saas_acceptance"]
    required_secrets:
      - ref: saas-test-session
        env: SAAS_TEST_SESSION_COOKIE
        same_value: true
```

HTTP/OpenAPI check 中实际会展开的 URL/header/body `{env:NAME}` 会被自动发现。需要把环境变量绑定到不同 reference 时，必须先在两端本机 `secrets.bindings` 授权，再在 draft 中显式声明：

```json
{
  "required_secrets": [
    {"ref": "saas-test-session", "env": "SAAS_TEST_SESSION_COOKIE", "same_value": true}
  ]
}
```

任务单最终只固化 `ref`、`env`、`same_value`；不包含值、路径或 proof。

## 创建与修订保证

当任务需要密钥时，`handoffctl create` 会在复制契约/附件、登记会话和写任务之前：

1. 使用与 verifier 相同的 resolver 检查发起端；
2. 向后端 `POST /v1/handoff/secret-preflight` 发送签名请求；
3. 确认后端也存在该 reference；
4. `same_value: true` 时，用通知控制面密钥派生、并绑定随机 challenge 的域分离 HMAC 比较值是否相同；即使预检运行在可信内网 HTTP 上，被动监听者也不能仅凭 challenge/proof 离线猜测低熵业务密钥；
5. 校验后端响应的时间戳、request ID 和 HMAC 签名。

本地缺失、后端缺失、值不一致、端点不可达、响应未认证或协议异常都会 fail closed，任务不会创建。无密钥任务不发起预检，保持原有离线行为。`amend` 和 `reverify` 同样执行预检，不能绕过创建保证。

## verifier 隔离

- HTTP/OpenAPI 占位使用任务固化且经本机 `secrets.bindings` 授权的 env→reference 绑定解析。
- 含密钥的 HTTP/OpenAPI check 只能访问与环境 `{base_url}` 同源的 URL，并完全禁止重定向，避免 header/body 被转发到外部 sink。
- 每个 registered suite/script 只注入自身声明的环境变量。
- suite/script 不继承父进程的任意环境，只保留启动所需的基础非敏感变量。
- verifier 在任何截断与持久化之前脱敏已知明文、JSON/Python 转义及 URL/form 编码形式。
- `PATH`、`LD_PRELOAD`、`PYTHONPATH`、`HANDOFF_NOTIFY_SECRET`、`GIT_CONFIG_*` 等控制面或进程劫持变量禁止声明为验收密钥。

## 运维建议

- 优先使用 Vault、1Password Connect、systemd credentials 或权限为 `0600` 的本机文件生成 `secrets.refs` 所指文件。
- session cookie 最好由专用测试账号在运行时登录生成，避免长期 cookie；若暂时使用静态 cookie，定期轮换。
- 先升级并重启后端 listener，再升级前端发起端；带 `required_secrets` 的新任务需要后端支持预检端点。
- proof 绑定单次 request/challenge。listener 的内存 receipt 仅提供时间窗口内的幂等与“同 ID 不同正文”拒绝；它不是跨进程重启的持久 replay ledger。预检本身是只读查询，认证与时间窗仍会对每次请求执行。

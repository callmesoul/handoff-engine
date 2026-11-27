# 前端（发起端）验收密钥配置清单

适用范围：创建带 `required_secrets`（例如 `SAAS_TEST_SESSION_COOKIE`）的交接任务前，发起端必须完成的本机配置。代码依据：`src/handoff/config.py`、`src/handoff/secrets.py`、`src/handoff/notification.py`。

> 密钥明文**不要**发给任何人、不要写进草稿/requirement/契约/附件/回报，也不要提交进 Git。只配置文件路径和引用名。

---

## 0. 先和后端对齐两件事（必须）

1. **public reference 名**：两端必须用**完全相同**的引用名（区分大小写）。后端此前收到的通知里示例用的是 `saas-test-session`；请先确认后端实际配的是这个名字，若不同，下面所有 `saas-test-session` 替换为后端的名字。
2. **环境变量名与同值要求**：确认是 `SAAS_TEST_SESSION_COOKIE`，且 `same_value: true`（两端值必须完全相同）。若后端用的是别的变量名或 `same_value: false`，也要同步调整。

> **后端不想手工抄 cookie？** 新版本支持“create 时安全推送”：后端在 local.yaml 打开 `secrets.provision_enabled`（并可选 `provision_refs: [saas-test-session]` 限制白名单），前端在 local.yaml 打开 `secrets.provision_push: true`（或 create 时加 `--provision-secrets`）。create 时前端会用通知共享密钥把 cookie 加密推给后端 listener，后端自动写入 `provision_dir/<ref>` 并更新 `refs.json`，随后立即可用于预检。任务单/Git/回报仍只有 ref、不存明文。后端需要拉取新代码并重启 listener 才支持此端点。

---

## 1. 放置 cookie 文件（权限 0600）

把 cookie 值写进一个本机文件，例如：

```bash
# 文件内容只有 cookie 值本身（末尾可以有换行，读取时会去掉 \r\n）
printf '%s' '<cookie 值>' > /home/callmesoul/.config/handoff/saas-test-session-cookie
chmod 600 /home/callmesoul/.config/handoff/saas-test-session-cookie
```

> 注意：`<cookie 值>` 由你本地填入，不要粘贴到聊天、草稿或任何会进入 Git 的文件。

---

## 2. 修改交接仓 `config/local.yaml`

交接仓路径：`$HOME/code/handoff-store`（软链到 `dropspot-handoff-store`）。

在现有 `secrets:` 段**新增** `refs` 和 `bindings`（旧的 `HANDOFF_NOTIFY_SECRET_FILE` / `OPENAPI_API_KEY_FILE` 保留不动）：

```yaml
secrets:
  HANDOFF_NOTIFY_SECRET_FILE: /home/callmesoul/.config/handoff/notify-secret.txt
  OPENAPI_API_KEY_FILE: /home/callmesoul/.config/handoff/openapi-api-key.txt

  # public ref → 本机 cookie 文件（路径可绝对路径，也可相对交接仓根）
  refs:
    saas-test-session: /home/callmesoul/.config/handoff/saas-test-session-cookie

  # HTTP/OpenAPI 的 {env:NAME} 占位：env → public ref 授权
  bindings:
    SAAS_TEST_SESSION_COOKIE: saas-test-session
```

规则（来自 `config.py` 的校验）：
- `refs` 的 key（引用名）匹配 `^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$`。
- `bindings` 的 key 必须是合法环境变量名且**大写**，不能是保留变量（`HANDOFF_NOTIFY_SECRET`、`PATH`、`LD_PRELOAD` 等）。
- `bindings` 引用的 ref 必须出现在 `refs` 里。

---

## 3. 通知控制面密钥（一般无需改动）

`HANDOFF_NOTIFY_SECRET` 用于预检请求/响应签名。当前配置：
- 环境变量 `HANDOFF_NOTIFY_SECRET` 未导出时，会回退读取 `secrets.HANDOFF_NOTIFY_SECRET_FILE` 指向的文件；
- 该文件已存在（约 64 字节，满足 ≥32 字节要求）。

**只需确认**：前端这个文件里的值与后端 listener 的 `HANDOFF_NOTIFY_SECRET` **完全一致**。不一致会导致预检签名校验失败。这个文件不要改路径、不要动内容，除非你确认要与后端重新对齐。

---

## 4. 预检目标地址（确认即可，一般无需改动）

`preflight_secrets` 从 `notifications.url` 同源派生 preflight 端点。当前：

```yaml
notifications:
  url: http://192.168.1.2:8788/v1/handoff/wake
```

会派生出 `http://192.168.1.2:8788/v1/handoff/secret-preflight`。请确认 `192.168.1.2:8788` 是后端已升级部署并重启的 listener 地址。若拓扑不同，可显式配置 `notifications.preflight_url`，路径必须精确为 `/v1/handoff/secret-preflight`。

---

## 5. 配置完成后我来核对（不读明文）

你配置好后回复我，我会重新读取 `local.yaml` 并只检查：
- `refs`/`bindings` 是否存在、格式是否合法；
- cookie 文件是否存在、权限是否合理（不读取、不回显 cookie 内容）；
- 通知密钥文件是否存在、长度是否达标（不回显内容）。

确认无误后，我再调用 `handoffctl create` 创建带密钥任务；创建时会自动向后端做双端预检，任一侧缺失或值不一致都会 fail closed，任务不会写入。

---

## 快速自检命令（你在 WSL 里执行，不回显明文）

```bash
cd "$HOME/code/handoff-store"
# 看配置结构（不含密钥内容）
sed -n '/^secrets:/,/^[^ ]/p' config/local.yaml
# 看文件是否存在与权限
ls -l /home/callmesoul/.config/handoff/saas-test-session-cookie /home/callmesoul/.config/handoff/notify-secret.txt
```

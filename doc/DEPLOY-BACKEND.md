# 后端部署（Windows）

这套东西的后端不是「一个服务」，是四块：

```text
前端机器            后端 worker 主机                       CI/CD                dev 环境
─────────          ────────────────────────────────      ──────────────       ─────────
handoffctl    ──▶   交接仓 clone（专用）                  按 SHA 部署          /version
create/push          ├─ worker 接收局域网通知、认领       环境级并发锁         返回完整 SHA
deliver       ◀──   ├─ 专用 clone + 每次 attempt 独立 worktree
验收报告             ├─ codex exec（无推送凭据）
                     └─ push 分支 + gh 开 PR
```

worker **不部署**，CI **不改任务状态**。

两种跑法，选一个：

| | 原生 Windows | WSL（同一台机器） |
|---|---|---|
| 要装 | git、Python、gh、codex | 已有（本机 WSL 里 git/gh/codex 都在） |
| 路径 | `C:\handoff\...` | `/home/you/handoff/...` |
| 常驻 | 计划任务 / NSSM | systemd（如果 WSL 启用了 systemd）或 `nohup` |
| git 性能 | 一般 | 好（放在 ext4，不要放 `/mnt/c`） |
| 适合 | 只能 Windows、要开机即跑 | 已经在这台机器上用 WSL 开发 |

下面先写原生 Windows；WSL 的差异单独列。

---

## 1. 原生 Windows：装依赖

在**管理员** PowerShell 里：

```powershell
winget install --id Git.Git -e
winget install --id GitHub.cli -e
# 可选：直接装 Codex 的独立安装包；也可以走 npm
npm install -g @openai/codex
```

验证（新开一个窗口）：

```powershell
git --version
gh --version
codex --version          # 走 npm 时其实是 %APPDATA%\npm\codex.cmd
python --version         # 需要 ≥ 3.10
```

`codex` 找不到，通常是 npm 全局 bin 不在 PATH（nvm 常见）。要么把
`%APPDATA%\npm` 加进 PATH，要么用 `HANDOFF_CODEX` 指绝对路径。

---

## 2. 两个 checkout

```powershell
# 代码：只用来跑 handoffctl，别在这里跑 worker
git clone <交接仓 URL> C:\handoff\app
cd C:\handoff\app
python -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install -r requirements.txt

# 数据：专用 clone，worker 会 fetch + reset --hard，必须干净
git clone <交接仓 URL> C:\handoff\store
```

两条硬规矩：

- **不要放 UNC 路径**（`\\wsl.localhost\...`、`\\nas\...`）。git worktree 和文件锁在 UNC 上会出各种奇怪问题。
- **根目录要短**。`C:\handoff` 而不是 `C:\Users\you\Documents\工作\交接系统\`。worktree 路径会叠三层，Windows 默认 260 字符上限很容易撞。

选短路径之外，再开长路径支持：

```powershell
git config --system core.longpaths true
```

为什么代码和数据分开：`TaskStore.sync()` 会 `reset --hard`。同一个目录里，一次 sync 可能把运行中的代码换掉，或者被代码改动卡住（"工作区有未提交改动"）。

业务仓不用预先 clone，worker 自己会在 `<store>\.handoff-run\repo-cache\` 下建缓存再开 worktree。

---

## 3. 账号与凭据

### 3.1 worker bot（GitHub）

独立账号，最小权限：

| 权限 | 要不要 | 说明 |
|---|---|---|
| 后端仓：推分支、开 PR | 要 | 只推 `codex/*` |
| 后端仓：合并、改保护规则 | **不要** | 合并是人的边界 |
| 交接仓：读写 | 要 | 任务单和事件在这 |
| 部署凭据、生产 secret | **不要** | worker 永远不部署 |

SSH key 放 `%USERPROFILE%\.ssh\`，`config` 里指定：

```sshconfig
Host github.com
  IdentityFile ~/.ssh/handoff_deploy
  IdentitiesOnly yes
```

```powershell
ssh -T git@github.com        # 应看到 "Hi <bot>! You've successfully authenticated"
```

`gh` 登录（PR 走 API，不走 SSH）：

```powershell
gh auth login --hostname github.com --git-protocol https --scopes repo
gh auth status
```

### 3.2 Codex

```powershell
codex login            # 浏览器授权
codex login status     # 应看到 Logged in using ...
```

凭据在 `%USERPROFILE%\.codex\`。**不要**改成服务账号或 SYSTEM 跑：那样读不到这份凭据，codex 会直接认证失败。

不要把这些放进环境变量：worker 本来就会把 `GITHUB_*`、`GH_*`、`*TOKEN*`、`*SECRET*`、`SSH_AUTH_SOCK` 从 Codex 子进程里剔掉，放文件里更干净。

### 3.3 行尾

Windows 上 `core.autocrlf=true` 会让 worker 提交时改行尾，diff 里凭空多出一堆改动。给 worker 用的 git 配置里关掉：

```powershell
git config --global core.autocrlf false
```

---

## 4. 配置

改 `C:\handoff\store\config\`，然后提交回去，别的机器看到同一份。

`repositories.yaml`：

```yaml
repositories:
  - id: your-org/api
    role: backend
    host: github
    default_branch: main
    allowed_base_refs: [dev]
    pr_base_ref: dev
    remote: git@github.com:your-org/api.git
    cache_dir: .handoff-run/repo-cache/your-org__api
  - id: your-org/web
    role: frontend
    host: github
    allowed_base_refs: [dev]
    remote: git@github.com:your-org/web.git
```

`codex.yaml`：无人值守常见组合

```yaml
codex:
  persist_thread: true     # 出事能 codex resume 进去看
  keep_worktree: true      # resume 需要 cwd
  timeout_seconds: 3600
```

机器专属路径不要写进共享配置，放进 `config/local.yaml`（已 gitignore）：

```yaml
repo_paths:
  your-org/api: C:/handoff/api        # 本机只读副本，验收时跑 repo_script / test_suite
codex:
  binary: C:/Users/you/AppData/Roaming/npm/codex.cmd

notifications:
  secret_env: HANDOFF_NOTIFY_SECRET
  listen_host: 0.0.0.0
  listen_port: 8788
  allowed_cidrs: [192.168.1.10/32]   # 只允许前端机器；部署时换成实际地址
  inbox_database: .handoff-run/notify/inbox.sqlite3
  outbox_database: .handoff-run/notify/outbox.sqlite3  # 回推 verified/failed 失败时持久重试
```

两端设置同一个至少 32 字节的随机密钥。Windows 当前用户可用 `setx HANDOFF_NOTIFY_SECRET "<random-secret>"`，设置后重新登录，让计划任务继承新环境。真实密钥禁止提交。

后端接收 `task.created` 时会把其中签名的前端 `reply_url` 只保存在本机 inbox。任务进入 `verified` 或 `failed` 并成功 push 交接仓后，CLI 只把 `task.completed` 写入本机 outbox，由常驻 notifier 签名并向回调地址发送；事件只有任务 ID、revision、store commit、状态和部署 SHA，前端仍必须从 Git 拉取任务事实。网络失败不会回滚终态，事件保留在 outbox 指数退避重试。

自检：

```powershell
C:\handoff\app\.venv-win\Scripts\python.exe -m handoff.cli --root C:\handoff\store doctor --role backend
```

0 失败再继续。它会查二进制、gh/codex 登录、交接仓、每个仓库的远端与分支、环境、检查登记表。

---

## 5. 常驻

### 5.1 计划任务（推荐，不需要额外软件）

```powershell
powershell -ExecutionPolicy Bypass -File C:\handoff\app\deploy\windows\install-task.ps1 `
    -StoreRoot C:\handoff\store -Worker backend-01 -Listen config
```

它注册 `HandoffWorker`：**登录时**启动、崩溃后每分钟重试、不设运行时长上限。日志写到
`C:\handoff\store\.handoff-run\logs\worker\worker-yyyyMMdd.log`，保留 14 天。

看日志：

```powershell
Get-Content C:\handoff\store\.handoff-run\logs\worker\worker-$(Get-Date -Format yyyyMMdd).log -Wait
```

正常会显示 `局域网通知监听` 和 `worker backend-01 启动，局域网通知唤醒（不轮询）`。有任务时依次
`已认领` → `codex ...` → `PR #n`。

监听端口需要最小权限防火墙规则，只允许前端机器 IP，不要放行整个公网或所有局域网设备。应用层还会校验 HMAC、时间戳、来源网段和 event ID；防火墙是额外边界，不能省略。

管理员 PowerShell 示例（地址必须替换为当前前端机器的固定地址）：

```powershell
New-NetFirewallRule -DisplayName "Handoff wake from frontend" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8788 `
  -RemoteAddress 192.168.1.10
```

先从前端访问 `http://<backend-ip>:8788/healthz`，确认返回 `{"ok":true}`；再发测试任务。`/healthz` 只证明网络可达，不代表 HMAC 配置正确。

**为什么是「登录时」而不是「开机时」**：worker 要用你的 gh / codex 凭据，它们在
`%USERPROFILE%` 下。用 SYSTEM 跑服务，那两份凭据不存在，PR 和 codex 全挂。

要求机器不锁屏也能跑？开自动登录，或者下面用 NSSM 把服务设成同一个用户。

### 5.2 NSSM（要真正的服务、不依赖登录）

需要管理员，并且给服务账号存密码：

```powershell
nssm install HandoffWorker "C:\handoff\app\.venv-win\Scripts\python.exe" `
  "-m handoff.cli --root C:\handoff\store worker --worker backend-01 --role backend --listen config"
nssm set HandoffWorker AppDirectory C:\handoff\store
nssm set HandoffWorker ObjectName .\youruser yourpassword
nssm set HandoffWorker AppEnvironmentExtra PYTHONPATH=C:\handoff\app\src PYTHONUTF8=1 PYTHONIOENCODING=utf-8
nssm set HandoffWorker AppExit Default Restart
nssm set HandoffWorker AppRestartDelay 15000
nssm start HandoffWorker
```

`ObjectName` 必须是登录过 codex/gh 的那个用户。

### 5.3 WSL

```bash
cd /home/you/handoff/store
nohup ../app/bin/handoffctl --root . worker --worker backend-01 --role backend \
  --listen config \
  > .handoff-run/worker.log 2>&1 &
```

WSL 里没有 systemd 时用这种方式；有 systemd 就照 `deploy/systemd/` 装。仓库放
`/home/...`（ext4），不要放 `/mnt/c`，git 在 9p 上很慢。

---

## 6. CI/CD：三条硬要求

1. **按 SHA 部署**：输入完整 commit SHA，禁止部署时从移动中的分支重新取代码。
2. **环境级互斥**：同一 `concurrency.group`，`cancel-in-progress: false`。
3. **`/version` 返回完整 SHA**：构建时注入，不要请求时 `git rev-parse`（容器里通常没有 `.git`）。

```
GET /version → 200
{"sha": "<完整 SHA>", "build_id": "...", "built_at": "...", "environment": "dev"}
```

模板 `deploy/ci/deploy-dev.example.yml`。Windows 上的 CI 只是跑在 GitHub 托管 runner，和 worker 机器无关。

合并 gate 由 PR 平台负责。`gh pr merge --auto` 成功只代表「已开启」，worker 会继续轮询到 `MERGED` 才写 `merge_sha`。

---

## 7. 首次闭环

前端发单后，在 worker 机器：

```powershell
Get-Content C:\handoff\store\.handoff-run\logs\worker\worker-$(Get-Date -Format yyyyMMdd).log -Wait
C:\handoff\app\.venv-win\Scripts\python.exe -m handoff.cli --root C:\handoff\store list --status awaiting_merge
```

人工合并 PR，CI 部署，然后：

```powershell
C:\handoff\app\.venv-win\Scripts\python.exe -m handoff.cli --root C:\handoff\store deliver <id>
```

终态 `verified` 时，三个 SHA 必须对得上：

- `result.head_sha`：PR 分支最后一个提交
- `result.merge_sha`：平台返回的合并提交
- `deployment.observed_sha`：`/version` 报的部署提交（可等于 `merge_sha`，也可为经 Git 提交图证明包含它的后续提交）

前端随后在自己的环境里验收，通过后运行 `handoffctl accept <id> --reason '前端验收通过'`（或点交接面板「前端已验收」按钮），任务转为 `frontend_accepted` 终态。这一步只是记录，不会另行通知后端。

---

## 8. 运维

**升级代码**（不动数据）：

```powershell
git -C C:\handoff\app pull
C:\handoff\app\.venv-win\Scripts\python.exe -m pip install -r C:\handoff\app\requirements.txt
Stop-ScheduledTask  -TaskName HandoffWorker
Start-ScheduledTask -TaskName HandoffWorker
```

**回滚**：同一个部署流程手动指定上一个 SHA。已 `verified` 的任务不要改回去，新版本要新开单——复用旧单会让审计断掉。

**任务卡在 `in_progress`**：租约到期自动被接管，也可以立刻：

```powershell
python -m handoff.cli --root C:\handoff\store expire-leases
```

**磁盘**：`persist_thread: true` 会保留 worktree，每个 attempt 一份。

```powershell
python -m handoff.cli --root C:\handoff\store clean-worktrees          # 只列
python -m handoff.cli --root C:\handoff\store clean-worktrees --apply  # 真删
```

只清 `verified` / `frontend_accepted` / `cancelled` / 任务单已不存在的目录。

**Defender**：codex 会在 worktree 里频繁读写。把 `C:\handoff` 加进排除项能少很多莫名其妙的卡顿。

---

## 9. 安全检查清单

- [ ] bot 只能推 `codex/*`，分支保护开着
- [ ] 交接仓写权限 ≠ 业务仓写权限
- [ ] worker 用户没有部署凭据、没有生产 secret
- [ ] 计划任务/服务用的是「登录过 codex 和 gh 的那个用户」
- [ ] Codex 子进程环境已剔除凭据（worker 默认行为）
- [ ] 生产环境不在 `environments.yaml`，永远不自动部署
- [ ] `acceptance.checks` 里没有裸 shell
- [ ] `C:\handoff` 不在共享盘、不在同步盘（OneDrive 会锁文件）

---

## 10. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `错误[git_error]: 命令不存在: git` | PATH 里没有 git | 设 `HANDOFF_GIT`，或把 Git 的 `cmd` 目录加进 PATH |
| `gh`/`codex` 找不到 | npm bin 不在 PATH | 设 `HANDOFF_GH` / `HANDOFF_CODEX` 绝对路径 |
| `codex` 秒退，提认证失败 | 服务跑在 SYSTEM 下，读不到用户凭据 | 换成登录用户跑（计划任务 / NSSM ObjectName） |
| 日志里中文乱码 | 控制台代码页 | `run-worker.ps1` 已设 `PYTHONUTF8=1`；手跑时也设 |
| 路径过长 / `Filename too long` | worktree 层级深 | 挪到短根目录，开 `core.longpaths` |
| 提交里凭空多出整文件改动 | `core.autocrlf=true` | 给 worker 用户设 `core.autocrlf false` |
| 任务一直 `open` | worker 没起来，或 `target.repo` 不属于该 role | 看日志，跑 `doctor --role backend` |
| `工作区有未提交改动` | 在带改动的 checkout 上跑了 worker | 用专用 store clone |
| `deliver` 报部署未对齐 | CI 尚未部署目标提交、`/version` 返回短 SHA，或执行端无法读取提交图 | 修 CI/仓库只读权限；版本接口必须返回完整 SHA |

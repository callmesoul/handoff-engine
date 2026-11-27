# 交接系统自身的自动更新

交接仓有新代码时，前端和后端机器自己拉取、重装依赖、重启服务。

**更新的是交接系统自己的核心代码（`handoffctl`、worker、listener、notifier），不是任务数据。** `tasks/`、`events/`、`contracts/`、`artifacts/` 的变化不触发任何动作——那些是数据，天天在变。业务仓（`your-org/api`、`your-org/web`）的部署是 CI 的事，见 `deploy/ci/deploy-dev.example.yml`，不在本文范围。

---

## 1. 为什么这件事在这里特殊

交接仓是**代码和数据混在同一个 Git 仓库**里（README：「Git 是唯一真实来源」）。一次 `git pull` 会同时带来：

- 别人发的任务单（`tasks/`）
- 审计事件（`events/`）
- 真正的代码改动（`src/`、`bin/`）

任务提交比代码提交频繁几个数量级。不做区分地「有新提交就更新」，结果就是一天被任务数据触发几十次重启，而且每次重启都打断正在跑的 worker。

所以第一件事是**判定什么算代码**，第二件事才是拉取和重启。

---

## 2. 什么算「核心代码更新」

用 `git diff --name-only <local>...<remote>` 判定。注意是**三点** diff：只取远端相对分叉点带来的变化，两点 diff 会把本地领先的提交（前端 create 后 push 失败）也算进来。

| 路径 | 分类 | 动作 |
|---|---|---|
| `src/**`、`bin/**`、`schemas/**` | 核心代码 | 拉取 + 重启 |
| `pyproject.toml`、`requirements.txt` | 依赖 | 拉取 + 重装 + 重启 |
| `config/*.yaml`（`local.yaml` 除外） | 共享配置 | 拉取 + 重启（配置启动时加载） |
| `.agents/skills/**` | 技能 | 拉取，**不重启**（符号链接，Codex 下次会话读到） |
| `dsh-handoff-panel/**` | 前端 DSH 插件 | 不由 Python 自更新器安装；仅前端部署按插件 README 独立升级 |
| `doc/**`、`README.md`、`scripts/**`、`tests/**`、`LICENSE` | 文档/测试 | 忽略，不拉 |
| `tasks/**`、`events/**`、`contracts/**`、`artifacts/**`、`.handoff-run/**` | 数据 | 忽略，永不触发 |

白名单式判定。新增一个未知目录不会误触发，比「排除数据目录」更稳。

---

## 3. 检测：轮询是兜底，不是唯一

轮询是基线——它是唯一不依赖外部可达性的方式。代价是延迟（5–10 分钟）。想更快，用事件驱动。三条路，按这套系统的拓扑排：

### 3.1 谁 push 谁通知（最贴这套系统）

代码 push 一定是某台机器上的人干的。谁 push 谁就知道代码变了，push 完直接通知对端——零延迟，走局域网里已有的 `notification.py` HMAC 通道，不引入新概念。

```text
前端 push 了代码 → 发一条 code.updated（带新 SHA）给后端
后端收到 → 三点 diff → 命中代码路径 → 更新
```

和 README 里「有通知时 worker 不轮询，通知立即唤醒」是同一套心智模型。局限：第三台机器 push 会漏，所以轮询仍是安全网。

### 3.2 公网 webhook（要有 tunnel 才成立）

GitHub 的 push 事件是零延迟，但 GitHub 打不进内网。有 cloudflared / tailscale funnel 常驻才能收。有的话就复用 `notification.py` 的验签、时间戳防重放、CIDR、幂等。没有就不值得为省这几分钟专门开公网口。

### 3.3 CI 打信号 ref（把判定从部署机挪走）

交接仓跑一个 Actions：push 时判定是否命中代码路径，命中就 `git push origin main:refs/heads/__deploy__`。部署机只盯这一个轻量 ref，`ls-remote` 一次 1KB，变了就更新。判定逻辑只在 CI 维护一份，部署机不重复实现。

代价：仍是轮询，只是轮询对象更轻；部署机要能读那个 ref。

### 3.4 无论选哪条，轮询都留着

webhook 会漏（网络抖动、重投失败、进程重启窗口），通知会丢。漏一条 webhook 等于环境停在旧版本，直到下次有人 push。所以轮询不能因为有了 webhook 就拆掉。

轮询本身也做了优化：先 `ls-remote` 只拿 ref SHA，和上次处理过的一样就直接退出，**不 fetch 对象**。无变化路径约 1KB、几毫秒。fetch 只在 ref 真动了之后发生。

```bash
git ls-remote origin refs/heads/main        # 1KB，先比
# 变了才 fetch + 三点 diff
```

间隔：后端 10 分钟，前端 5 分钟。等的是人写代码，不是等任务事件；核心代码一天改几次，60 秒轮询只徒增 fetch。

用 timer 不用 cron：cron 不关心上次跑完没有。合并 + `pip install` + 重启可能超过 10 分钟，叠起来就两个进程抢锁（脚本用 `O_EXCL` 挡，但白跑一趟）。

---

## 4. 执行

```
fetch → 三点 diff → 分类
  ├─ 只有数据/文档 → 退出
  ├─ 后端有 in_progress 任务 → 推迟（超 DEFER_MAX 强制）
  ├─ 工作区脏 / 分叉 → 拒绝，交人
  └─ git merge --ff-only → 按需 pip install → 重启 → doctor 自证
```

三条不能妥协的：

- **不用 `reset --hard` 更新**。工作区脏就拒绝，绝不冲掉人的改动。`reset --hard` 只用于回滚到更新前的已知 SHA。
- **不用 `git pull`**。pull 会 merge，本地领先时产出一棵没人测过的树。只用 `fetch` + `merge --ff-only`；分叉就拒绝。
- **依赖重装先打快照**。`pip freeze` 存一份，`pip install` 失败就恢复快照再回滚代码。顺序是「先回滚代码，再恢复依赖，再重启」，保证回滚后进程加载的是旧代码配旧依赖。

### 回滚

任何一步挂了都回到更新前的 SHA：

```text
git reset --hard <old_sha> → 恢复依赖 → 重启 → 记 fail_count
```

回滚本身也失败时写 `$ROOT/.broken`，别假装成功。

### 熔断

同一个提交连续失败 3 次就停手，返回 0（不惊动 systemd）。坏了就是坏了，等人来看，别每 10 分钟重试一次把日志刷爆。

---

## 5. 安全重启

### 前端

重启的是 `frontend-listener` 和 `notifier`。两者都无状态：inbox/outbox 在 SQLite 里，重启后重放，幂等。所以随时可以重启，不用等任何东西。这也是前端间隔能比后端短的原因。

### 后端

重启 `worker` 会杀掉正在跑的 `codex exec`。后果：任务停在 `in_progress`，租约到期后重新认领，attempt + 1，codex 白烧一轮额度。

所以后端更新前先查有没有任务在跑：

```text
读 store/tasks/*.json，有 claimed / in_progress → 推迟
推迟超过 DEFER_MAX（默认 6 小时）→ 强制更新，宁可丢一轮 attempt
```

注意：读本地 `tasks/` 目录，不要调 `handoffctl list`——那会触发 `TaskStore.sync()`，和 worker 抢同一个 checkout。脚本直接读 JSON 文件。

`indexer` 是 timer，每次跑完就退，不用管。`panel` 是长驻，重启断浏览器页面，可接受。

### 谁重启谁

`handoff-worker` 是 system service，普通用户没有 `systemctl restart` 权限。两条路：

- updater 以 root 跑，只对这几个 unit 放权（sudoers 白名单）
- 或者让 updater 进程本身就有权（root timer），内部 drop 到 handoff 用户跑 git/pip

选哪种取决于机器，但**不能让 worker 自己停自己**——它会停到一半被 SIGTERM 干掉。

---

## 6. 引擎与交接仓必须分离（硬前置）

自动更新只能作用于无任务数据的引擎 checkout。推荐布局：

```text
~/code/handoff-store        # 私有数据仓，--root / HANDOFF_ROOT 指向它
~/code/handoff-engine       # 引擎代码与虚拟环境，自动更新对象
```

前端 `create` 会在数据仓产生提交，`TaskStore.sync()` 也会同步数据仓；因此自更新脚本绝不能对数据仓执行引擎升级、替换或回滚。CLI、skills 和服务单元来自引擎仓，但每次调用都用 `--root "$HANDOFF_ROOT"` 指向数据仓。

前后端均使用相同边界，安装命令见 `doc/DEPLOY-AGENT.md`。

---

## 7. 自举：脚本不能留在被更新的目录里

updater 自己会 `git merge`。如果它放在 `app/` 里，合并会覆盖正在执行的脚本文件。

所以 `selfupdate.py` 要装到**仓库之外**：

```text
后端   /opt/handoff/selfupdate/selfupdate.py
前端   ~/.local/lib/handoff-selfupdate/selfupdate.py
```

安装脚本只做一次，之后更新它自己（需要时）走同样的自举流程，不依赖 app 的代码。它只用标准库，不 import `handoff`。

---

## 8. 已交付的文件

| 文件 | 用途 |
|---|---|
| `deploy/autoupdate/selfupdate.py` | 核心实现，跨平台，标准库，不 import handoff |
| `deploy/systemd/handoff-selfupdate.{service,timer}` | 后端，system service，10 分钟 |
| `deploy/systemd/handoff-selfupdate-user.{service,timer}` | 前端，user service，5 分钟 |
| `deploy/windows/install-selfupdate-task.ps1` | Windows 计划任务（后端） |
| `deploy/windows/run-selfupdate.ps1` | 计划任务的包装，负责设环境变量 |
| `deploy/selfupdate.env.example` | 机器专属配置模板 |

### 环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `APP` | 是 | 代码 checkout |
| `STORE` | 后端 | 交接仓，查 in_progress 用 |
| `BRANCH` | 否 | 默认 `main` |
| `ROLE` | 是 | `frontend` / `backend` |
| `SERVICES` | 是 | 空格分隔的 unit 名；`user:` 前缀表 systemctl --user；Windows 填计划任务名 |
| `PY` | 否 | 解释器，留空自动找 `.venv` / `.venv-win` |
| `DEFER_MAX` | 否 | 默认 6 小时 |
| `FORCE` | 否 | `1` 跳过 in_progress 检查 |

### 退出码

`0` 无变更/成功/熔断 · `3` 有别的实例 · `4` 被 in_progress 推迟 · `5` 脏或分叉拒绝 · `6` 失败已回滚。

---

## 9. 注意事项

1. **三点 diff**，不是两点。否则前端本地领先的提交会被误判成「要更新」。
2. **`merge --ff-only`**，分叉拒绝，绝不 `reset --hard` 覆盖人的改动。
3. **读本地 tasks/ 判 in_progress**，不调 CLI，避免触发 store sync。
4. **`pip freeze` 快照**先于重装；回滚顺序是代码→依赖→重启。
5. **doctor 只当观测，不当回滚条件**。它会报与本次更新无关的环境缺口，当门禁会回滚掉一次「其实成功」的更新。
6. **Windows 计划任务用 `MultipleInstances IgnoreNew`**，等价于锁。
7. **`nssm` 服务名不能传进 `SERVICES`**——脚本用 `Stop-ScheduledTask`/`Start-ScheduledTask`。NSSM 场景在 `run-selfupdate.ps1` 里换成 `nssm restart`。
8. **`config/local.yaml` 是 gitignore 的**，diff 里不会出现，但判定里仍显式排除，防手滑把它提交了。
9. **skill 更新不重启**：符号链接指向它，Codex 下次会话自动读到。但已经跑着的 Codex 会话不会热重载。
10. **回滚也失败写 `.broken`**，外部监控只认这个。

---

## 10. 验收

1. 往交接仓推一个只改 `tasks/` 的提交 → 前端和后端机器都不重启。
2. 推一个改 `src/` 的提交 → 5 分钟内前端重启 listener，10 分钟内后端重启 worker，`doctor` 无新缺口。
3. 推一个坏 `requirements.txt` → 自动回滚到旧 SHA，服务可访问，连续 3 次后熔断停手。
4. 后端有任务 `in_progress` 时推代码 → 更新被推迟，任务跑完才重启；`FORCE=1` 时立即更新。
5. 本地脏工作区 → 拒绝更新，日志有明确提示，改动原样保留。

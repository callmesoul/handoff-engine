"""配置自检。

`create` 失败一次只暴露一个问题，而且往往死在很后面。
`doctor` 一次性把「环境缺什么」列清楚：二进制、交接仓、仓库远端与分支、环境、检查登记表。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import executil, notification
from .config import Config
from .dsh_bridge import DshBridge, bridge_token, discover_bridge, token_file
from .errors import ConfigError, ExecutionError
from .gitcmd import git, run as run_cmd
from .sanitize import clean

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MARK = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _binary(name: str, env_var: str, *, required: bool, hint: str) -> Check:
    resolved = executil.resolve(name, env_var=env_var)
    if resolved:
        return Check(f"bin:{name}", OK, resolved)
    return Check(f"bin:{name}", FAIL if required else WARN, f"找不到；{hint}")


def _auth_checks() -> list[Check]:
    checks: list[Check] = []

    gh = executil.resolve("gh", env_var="HANDOFF_GH")
    if gh:
        res = run_cmd([gh, "auth", "status"], timeout=15, check_env=False)
        if res.ok:
            accounts = [line.strip() for line in res.stdout.splitlines() if "account" in line]
            checks.append(Check("auth:gh", OK, accounts[0] if accounts else "gh 已登录"))
        else:
            reason = clean((res.stderr or res.stdout).strip(), 200).splitlines()
            checks.append(
                Check("auth:gh", WARN, f"gh 未登录：{reason[0] if reason else 'gh auth login 后重试'}")
            )

    codex = executil.resolve("codex", env_var="HANDOFF_CODEX")
    if codex:
        res = run_cmd([codex, "login", "status"], timeout=20, check_env=False)
        text = clean((res.stdout or res.stderr).strip(), 200).splitlines()
        first = text[0] if text else "codex login status 无输出"
        checks.append(Check("auth:codex", OK if res.ok else WARN, first))
    return checks


_BRIDGE_SOURCE = {
    "config": "显式配置 notifications.dsh_bridge_url",
    "endpoint": "插件自公布（端口变了也跟得上）",
    "env": "启动时的 DSH_WEB_URL",
}


def _dsh_bridge_checks(config: Config, role: str | None = None) -> list[Check]:
    """前端回推 DSH 会话所需的三件事：地址、令牌、插件是否在听。

    地址按可靠性顺序找：显式配置 → 插件自公布的
    `$DSH_HOME/handoff-bridge.json` → `DSH_WEB_URL`。端口每次 DSH 重启都会变，
    所以自公布的端点文件是常态路径，另两条是兜底。令牌来自
    notifications.dsh_bridge_token_env 指定的环境变量，或插件写的
    `$DSH_HOME/handoff-bridge.token`。任一缺失，完成事件只会本地 pending。
    """
    if role not in (None, "frontend"):
        return []
    checks: list[Check] = []
    try:
        found = discover_bridge(config.notifications.dsh_bridge_url)
    except ConfigError as exc:
        checks.append(Check("dsh:bridge", WARN, exc.message))
        return checks
    source = _BRIDGE_SOURCE.get(found.source, found.source)
    checks.append(Check("dsh:bridge", OK, f"{found.url}（来源：{source}）"))

    file = token_file()
    try:
        bridge_token(config.notifications.dsh_bridge_token_env)
    except ConfigError:
        checks.append(
            Check(
                "dsh:bridge-token",
                WARN,
                f"没有桥接令牌；在 DSH 里启用 dsh-handoff-bridge 插件后它会写入 {file}",
            )
        )
    else:
        checks.append(Check("dsh:bridge-token", OK, f"已就绪（值不显示，来自 {file} 或环境变量）"))

    checks.append(_bridge_liveness_check(found))
    return checks


def _bridge_liveness_check(found) -> Check:
    """打一次 /health，确认那个端口上真有插件在听。

    端点文件是上一次 DSH 运行时写的；DSH 崩过或插件被卸载后，文件还在但端口
    早没了。只看文件会给出「一切正常」的假象，所以这里实地探一次。
    """
    try:
        DshBridge(found.url, "unused", timeout=3.0, origin=found).health()
    except ExecutionError as exc:
        return Check("dsh:bridge-live", WARN, f"插件没有响应：{exc.message}")
    return Check("dsh:bridge-live", OK, "插件在线")


def _store_checks(config: Config) -> list[Check]:
    root = Path(config.root)
    checks: list[Check] = []

    inside = git(root, "rev-parse", "--git-dir")
    if not inside.ok:
        checks.append(Check("store:git", FAIL, f"{root} 不是 git 仓库"))
        return checks
    checks.append(Check("store:git", OK, str(root)))

    dirty = git(root, "status", "--porcelain", "-uno").stdout.strip()
    if dirty:
        checks.append(Check("store:clean", WARN, "有未提交的已跟踪改动；自动化 sync 会拒绝 reset --hard"))
    else:
        checks.append(Check("store:clean", OK, "工作区干净"))

    remote = git(root, "remote", "get-url", config.store.remote)
    if remote.ok:
        checks.append(Check("store:remote", OK, remote.stdout.strip()))
    else:
        checks.append(
            Check(
                "store:remote",
                WARN,
                f"没有 {config.store.remote} 远端；只在本地提交，不会推给任何人",
            )
        )
    return checks


def _repo_checks(config: Config, role: str | None = None) -> list[Check]:
    checks: list[Check] = []
    for repo_id, repo in config.repositories.items():
        if role is not None and repo.role != role:
            continue
        if repo.role == "backend":
            if repo.scope is None:
                checks.append(
                    Check(
                        f"repo:{repo_id}:scope",
                        FAIL,
                        "未配置 scope 模板；新任务会 fail closed，请在 config/repositories.yaml 中配置",
                    )
                )
            else:
                checks.append(
                    Check(
                        f"repo:{repo_id}:scope",
                        OK,
                        f"默认 profile {repo.scope.default_profile} · {len(repo.scope.profiles)} 个 profiles",
                    )
                )
        listing = git(".", "ls-remote", "--heads", repo.remote_url, timeout=30)
        if not listing.ok:
            first = (listing.stderr or listing.stdout).strip().splitlines()
            reason = first[-1] if first else f"exit {listing.returncode}"
            checks.append(
                Check(
                    f"repo:{repo_id}",
                    FAIL,
                    f"{repo.remote_url} 不可达（{reason}）；config/repositories.yaml 的 id/remote 可能还是示例值",
                )
            )
            continue
        branches = {
            line.split("refs/heads/", 1)[1]
            for line in listing.stdout.splitlines()
            if "refs/heads/" in line
        }
        missing = [ref for ref in repo.allowed_base_refs if ref not in branches]
        if missing:
            checks.append(
                Check(
                    f"repo:{repo_id}",
                    FAIL,
                    f"远端缺少分支 {', '.join(missing)}（现有：{', '.join(sorted(branches)) or '无'}）",
                )
            )
        else:
            checks.append(Check(f"repo:{repo_id}", OK, f"{repo.remote_url} · {', '.join(repo.allowed_base_refs)}"))
    return checks


def _env_checks(config: Config) -> list[Check]:
    checks: list[Check] = []
    for name, env in config.environments.items():
        if not env.url:
            checks.append(Check(f"env:{name}", WARN if not env.auto_deploy else FAIL, "未配置 url，无法对齐部署版本"))
            continue
        detail = f"{env.version_url()}"
        if env.shared:
            detail += "（共享环境，验收可能遇到版本前移）"
        checks.append(Check(f"env:{name}", OK, detail))
    return checks


def _checks_registry(config: Config, role: str | None = None) -> list[Check]:
    checks: list[Check] = []
    repo_paths = config.checks.verifier.repo_paths
    for table_name, table in (("repo_scripts", config.checks.repo_scripts), ("test_suites", config.checks.test_suites)):
        for name, spec in table.items():
            repo = config.repositories.get(spec.repo)
            if role is not None and (repo is None or repo.role != role):
                continue
            if spec.repo not in repo_paths:
                checks.append(
                    Check(
                        f"check:{name}",
                        WARN,
                        f"{table_name} 已登记，但 verifier.repo_paths 没有 {spec.repo} 的本地路径，验收时会报 error",
                    )
                )
            else:
                checks.append(Check(f"check:{name}", OK, spec.repo))
    return checks


def _notification_checks(config: Config, role: str | None = None) -> list[Check]:
    cfg = config.notifications
    checks: list[Check] = []

    if role in (None, "frontend"):
        if not cfg.url:
            checks.append(Check("notify:sender", WARN, "未配置 notifications.url；create 只写交接仓"))
        else:
            parts = urlsplit(cfg.url)
            valid = parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.username and not parts.password
            if not valid:
                checks.append(Check("notify:sender", FAIL, "notifications.url 不是合法的无凭据 HTTP(S) URL"))
            elif parts.path != "/v1/handoff/wake":
                checks.append(Check("notify:sender", FAIL, "notifications.url 路径必须是 /v1/handoff/wake"))
            else:
                checks.append(Check("notify:sender", OK, cfg.url))

    receiver_enabled = cfg.listen_host not in ("127.0.0.1", "localhost", "::1") or bool(cfg.allowed_cidrs)
    if role in (None, "backend"):
        if not receiver_enabled:
            checks.append(Check("notify:receiver", WARN, "未配置局域网监听；worker 仍使用轮询"))
        else:
            bad = []
            for item in cfg.allowed_cidrs:
                try:
                    ipaddress.ip_network(item, strict=False)
                except ValueError:
                    bad.append(item)
            if not cfg.allowed_cidrs:
                checks.append(Check("notify:receiver", FAIL, "局域网监听必须配置 allowed_cidrs"))
            elif bad:
                checks.append(Check("notify:receiver", FAIL, f"allowed_cidrs 非法: {', '.join(bad)}"))
            else:
                checks.append(
                    Check(
                        "notify:receiver",
                        OK,
                        f"{cfg.listen_host}:{cfg.listen_port} · {', '.join(cfg.allowed_cidrs)}",
                    )
                )

    if cfg.url or receiver_enabled:
        try:
            notification.load_secret(cfg)
        except ConfigError:
            if role == "frontend":
                checks.append(
                    Check(
                        "notify:secret",
                        WARN,
                        "当前进程未获得通知密钥；create 只入队不受影响，"
                        "请在 notifier/listener 服务中验证 credential",
                    )
                )
            else:
                checks.append(
                    Check(
                        "notify:secret",
                        FAIL,
                        f"{cfg.secret_env} 或 {cfg.secret_env}_FILE 指定的密钥缺失或少于 32 字节",
                    )
                )
        else:
            checks.append(Check("notify:secret", OK, "通知密钥来源可用（值不显示）"))
    return checks


def _completion_backlog_check(config: Config) -> Check:
    """终态任务却有没发出去的完成通知——09c 那种静默漏发必须能被看见。

    这里只统计、不补发：补发要逐个反查前端回程地址，属于人工确认动作
    （handoffctl reconcile-completed --apply）。
    """
    from .store import TaskStore  # 延迟导入，避免 doctor ↔ store 循环

    store = TaskStore(config.root, config, push=False)
    outbox = notification.Outbox(config.notifications.outbox_path(Path(config.root)))
    terminal = store.peek_tasks(status=["verified", "failed"])
    missing = [t["id"] for t in terminal if not outbox.last_payload(t["id"], "task.completed")]
    if not missing:
        return Check("notify:completion-backlog", OK, f"{len(terminal)} 个终态任务的完成通知都有记录")
    sample = ", ".join(missing[:3]) + ("…" if len(missing) > 3 else "")
    return Check(
        "notify:completion-backlog",
        WARN,
        f"{len(missing)} 个终态任务从没入队过完成通知（{sample}）；"
        "核对后跑 handoffctl reconcile-completed --apply 补发",
    )


def run(config: Config, *, role: str | None = None) -> list[Check]:
    checks = [
        _binary("git", "HANDOFF_GIT", required=True, hint="安装 git，或设 HANDOFF_GIT"),
        _binary("gh", "HANDOFF_GH", required=False, hint="合并与 PR 需要；设 HANDOFF_GH 或 gh auth login"),
        _binary("codex", "HANDOFF_CODEX", required=False, hint="worker 执行需要；设 HANDOFF_CODEX 或 --codex-bin"),
    ]
    checks.extend(_auth_checks())
    checks.extend(_dsh_bridge_checks(config, role=role))
    checks.extend(_store_checks(config))
    checks.extend(_repo_checks(config, role=role))
    checks.extend(_env_checks(config))
    checks.extend(_checks_registry(config, role=role))
    checks.extend(_notification_checks(config, role=role))
    checks.append(_completion_backlog_check(config))
    return checks


def summarize(checks: list[Check]) -> dict:
    failed = [c for c in checks if c.status == FAIL]
    warned = [c for c in checks if c.status == WARN]
    return {
        "ok": not failed,
        "failed": len(failed),
        "warnings": len(warned),
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
    }


def render(checks: list[Check]) -> str:
    width = max((len(c.name) for c in checks), default=10)
    lines = [f"{_MARK[c.status]}  {c.name.ljust(width)}  {c.detail}" for c in checks]
    failed = [c for c in checks if c.status == FAIL]
    warned = [c for c in checks if c.status == WARN]
    lines.append("")
    lines.append(f"{len(checks)} 项检查：{len(failed)} 失败，{len(warned)} 警告")
    return "\n".join(lines)

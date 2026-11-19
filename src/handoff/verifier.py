"""结构化验收。

任务单里的 acceptance.checks 只能是这四类之一：
    http          —— 方法、URL、期望状态、JSON 断言
    openapi_diff  —— 两份明确来源的契约对比
    repo_script   —— 只允许 config/checks.yaml 注册过的脚本
    test_suite    —— 只允许 config/checks.yaml 注册过的套件

不接受任意 shell 字符串。命令一律 argv 执行，不经过 shell。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from .config import Config, SuiteSpec
from . import secrets as secret_files
from .errors import ExecutionError
from .gitcmd import run
from .http_client import HttpClient
from .sanitize import clean
from .timeutil import iso

_HTTP_VERBS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_TOKEN_RE = re.compile(r"\.([A-Za-z0-9_\-]+)|\[\s*(\d+)\s*\]|\[\s*'([^']*)'\s*\]|\[\s*\"([^\"]*)\"\s*\]")
# {env:NAME} —— URL/header 里的密钥占位，验收时从进程环境解析；任务单里只存占位不落明文。
_ENV_TOKEN_RE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class CheckResult:
    id: str
    status: str
    started_at: str
    duration_ms: int = 0
    output: str = ""
    observed_sha: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "output": self.output,
            "observed_sha": self.observed_sha,
        }


def jsonpath_get(data, path: str):
    """极简 JSONPath：$.a.b[0].c 与 $['a']['b']。找不到抛 KeyError。"""
    if not path.startswith("$"):
        raise KeyError(f"JSONPath 必须从 $ 开始: {path}")
    tokens: list[str | int] = []
    for match in _TOKEN_RE.finditer(path[1:]):
        key, index, single, double = match.groups()
        if index is not None:
            tokens.append(int(index))
        else:
            tokens.append(key or single or double or "")
    current = data
    for token in tokens:
        if isinstance(current, list) and isinstance(token, int):
            current = current[token]
        elif isinstance(current, dict):
            current = current[str(token)]
        else:
            raise KeyError(f"{path}: 无法在 {type(current).__name__} 上取 {token!r}")
    return current


class Verifier:
    def __init__(
        self,
        root: str | Path,
        config: Config,
        *,
        client: HttpClient | None = None,
        logger: Callable[[str], None] | None = None,
    ):
        self.root = Path(root)
        self.config = config
        self.client = client or HttpClient(
            timeout=config.verify_poll.request_timeout_seconds,
            deny_cidrs=config.checks.verifier.deny_cidrs,
        )
        self.log = logger or (lambda _msg: None)
        self.limit = config.checks.verifier.output_limit_chars

    # ------------------------------------------------------------------
    def run(self, task: dict, *, base_url: str | None = None, observed_sha: str | None = None) -> list[CheckResult]:
        env = self.config.env(task["acceptance"]["environment"])
        base = (base_url or env.url or "").rstrip("/")
        results: list[CheckResult] = []
        for check in task["acceptance"]["checks"]:
            self.log(f"检查 {check['id']}（{check['type']}）")
            results.append(self.run_one(check, task, base, observed_sha))
        return results

    def run_one(self, check: dict, task: dict, base_url: str, observed_sha: str | None) -> CheckResult:
        started_at = iso()
        started = time.monotonic()
        try:
            kind = check["type"]
            if kind == "http":
                status, output = self._http(check, base_url)
            elif kind == "openapi_diff":
                status, output = self._openapi_diff(check, base_url)
            elif kind == "repo_script":
                status, output = self._suite(check, task, script=True)
            elif kind == "test_suite":
                status, output = self._suite(check, task, script=False)
            else:
                status, output = "error", f"未知检查类型: {kind}"
        except Exception as exc:  # noqa: BLE001 - 单条检查失败不能拖垮整轮验收
            status, output = "error", f"{type(exc).__name__}: {exc}"
        duration = int((time.monotonic() - started) * 1000)
        result = CheckResult(
            id=check["id"],
            status=status,
            started_at=started_at,
            duration_ms=duration,
            output=clean(output, self.limit),
            observed_sha=observed_sha,
        )
        self.log(f"  → {result.status}（{duration}ms）")
        return result

    # -- http ----------------------------------------------------------
    def _http(self, check: dict, base_url: str) -> tuple[str, str]:
        url = _substitute(check["url"], base_url, self.config)
        headers = {k: _substitute(v, base_url, self.config) for k, v in (check.get("headers") or {}).items()}
        body = check.get("body")
        if body is not None:
            body = _substitute(body, base_url, self.config)
        timeout = float(check.get("timeout_seconds") or self.config.verify_poll.request_timeout_seconds)
        client = self.client if client_timeout_is(self.client, timeout) else HttpClient(
            timeout=timeout, deny_cidrs=self.config.checks.verifier.deny_cidrs
        )
        resp = client.request(check["method"], url, headers=headers, body=body)
        shown = _display_url(check["url"], base_url)
        lines = [f"{check['method']} {shown} → HTTP {resp.status}（期望 {check['expect_status']}）"]
        if resp.status != check["expect_status"]:
            lines.append("响应体：\n" + clean(resp.body, 2000))
            return "failed", "\n".join(lines)

        assertions = check.get("expect_jsonpath") or []
        if assertions:
            try:
                payload = resp.json()
            except ValueError:
                return "failed", "\n".join(lines + ["响应不是 JSON，无法做 JSONPath 断言"])
            for assertion in assertions:
                path = assertion["path"]
                try:
                    value = jsonpath_get(payload, path)
                except (KeyError, IndexError, TypeError) as exc:
                    if assertion.get("exists") is False:
                        lines.append(f"PASS {path} 不存在（符合断言）")
                        continue
                    lines.append(f"FAIL {path} 取值失败: {exc}")
                    return "failed", "\n".join(lines)
                if assertion.get("exists") is False:
                    lines.append(f"FAIL {path} 本应不存在，实际存在")
                    return "failed", "\n".join(lines)
                if "equals" in assertion and value != assertion["equals"]:
                    lines.append(f"FAIL {path} = {value!r}，期望 {assertion['equals']!r}")
                    return "failed", "\n".join(lines)
                if "matches" in assertion and not re.search(assertion["matches"], str(value)):
                    lines.append(f"FAIL {path} = {value!r}，不匹配 /{assertion['matches']}/")
                    return "failed", "\n".join(lines)
                lines.append(f"PASS {path} = {value!r}")
        return "passed", "\n".join(lines)

    # -- openapi -------------------------------------------------------
    def _openapi_diff(self, check: dict, base_url: str) -> tuple[str, str]:
        expected_path = self.root / check["expected_path"]
        if not expected_path.is_file():
            return "error", f"期望契约文件不存在: {check['expected_path']}"
        expected = _load_spec(expected_path)
        shown = _display_url(check["actual_url"], base_url)
        actual_url = _substitute(check["actual_url"], base_url, self.config)
        timeout = float(check.get("timeout_seconds") or self.config.verify_poll.request_timeout_seconds)
        client = self.client if client_timeout_is(self.client, timeout) else HttpClient(
            timeout=timeout, deny_cidrs=self.config.checks.verifier.deny_cidrs
        )
        resp = client.request("GET", actual_url, headers={"Accept": "application/json, application/yaml"})
        if resp.status >= 400:
            return "failed", f"{shown} 返回 HTTP {resp.status}"
        try:
            actual = yaml.safe_load(resp.body)
        except yaml.YAMLError as exc:
            return "failed", f"实际契约无法解析: {exc}"
        problems = diff_specs(expected, actual, allow_additions=bool(check.get("allow_additions", True)))
        if problems:
            return "failed", "\n".join(problems[:40])
        return "passed", f"契约一致（{len(expected.get('paths') or {})} 个路径）"

    # -- 登记的脚本 / 套件 ----------------------------------------------
    def _suite(self, check: dict, task: dict, *, script: bool) -> tuple[str, str]:
        table = self.config.checks.repo_scripts if script else self.config.checks.test_suites
        spec: SuiteSpec | None = table.get(check["name"])
        if spec is None:
            return "error", f"检查未登记: {check['type']} {check['name']}"
        if spec.repo != check["repo"]:
            return "error", f"登记表里 {check['name']} 属于 {spec.repo}，任务单写的是 {check['repo']}"

        workdir = self._repo_dir(spec.repo)
        if workdir is None:
            return "error", f"未在 config/checks.yaml 的 verifier.repo_paths 配置 {spec.repo} 的本地路径"

        argv = list(spec.argv)
        if script:
            target = Path(argv[0])
            if not target.is_absolute():
                target = workdir / target
            if not target.is_file():
                return "error", f"脚本不存在: {target}"
            argv = _interpreter_for(target) + [str(target)]

        timeout = float(check.get("timeout_seconds") or spec.timeout_seconds)
        result = run(argv, cwd=workdir / spec.cwd, timeout=timeout, check_env=False)
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        return ("passed" if result.ok else "failed"), clean(output.strip() or f"exit {result.returncode}", self.limit)

    def _repo_dir(self, repo: str) -> Path | None:
        configured = self.config.checks.verifier.repo_paths.get(repo)
        if not configured:
            return None
        path = Path(configured)
        if not path.is_absolute():
            path = self.root / path
        return path if path.is_dir() else None


def client_timeout_is(client: HttpClient, timeout: float) -> bool:
    return abs(float(getattr(client, "timeout", 0)) - float(timeout)) < 1e-6


_SECRET_CACHE: dict[str, str] = {}


def _secret_file_candidates(name: str) -> list[Path]:
    """`DOCS_API_KEY` → secrets/docs-api-key.txt（也接受下划线命名）。

    从 cwd 和模块位置向上找 `secrets/`，覆盖 app/ 工作目录和仓库根两种启动方式。
    """
    stems = [name.lower().replace("_", "-"), name.lower()]
    roots: list[Path] = []
    try:
        roots.append(Path.cwd())
    except OSError:
        pass
    here = Path(__file__).resolve()
    roots.extend(here.parents[:4])
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        for base in (root / "secrets", root.parent / "secrets"):
            if base in seen:
                continue
            seen.add(base)
            for stem in stems:
                out.append(base / f"{stem}.txt")
    return out


def _secret_from_file(name: str) -> str:
    if name in _SECRET_CACHE:
        return _SECRET_CACHE[name]
    for path in _secret_file_candidates(name):
        try:
            if not path.is_file():
                continue
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            _SECRET_CACHE[name] = value
            return value
    return ""


#: verifier 的 {env:DOCS_API_KEY} 与配置里的 OPENAPI_API_KEY 是同一把 key。
_VERIFIER_SECRET_ALIASES = {"DOCS_API_KEY": "OPENAPI_API_KEY"}


def _resolve_env(name: str, config: "Config | None" = None) -> str:
    """取值优先级：进程环境变量 → 配置 *FILE 指向的文件 → secrets/<name>.txt。

    这样从面板起任务时，验收脚本不必依赖启动进程的系统环境；不传 config 时
    兼容旧的独立调用与测试。
    """
    value = os.environ.get(name)
    if value:
        return value
    canonical = _VERIFIER_SECRET_ALIASES.get(name, name)
    if config is not None:
        configured = secret_files.config_secret(config, canonical)
        if configured:
            return configured
    return _secret_from_file(name)


def _substitute(text: str, base_url: str, config: "Config | None" = None) -> str:
    out = text.replace("{base_url}", base_url) if base_url else text

    def _env(match: re.Match) -> str:
        name = match.group(1)
        value = _resolve_env(name, config)
        if not value:
            raise ExecutionError(f"环境变量 {name} 未配置，无法解析占位 {match.group(0)}")
        return value

    return _ENV_TOKEN_RE.sub(_env, out)


def _display_url(text: str, base_url: str) -> str:
    """输出里只展开 {base_url}，{env:*} 保持占位——密钥不落任务单。"""
    return text.replace("{base_url}", base_url) if base_url else text


def _interpreter_for(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".sh":
        return ["bash"]
    if suffix == ".py":
        return [sys.executable or "python3"]
    if suffix in (".js", ".mjs", ".cjs"):
        return ["node"]
    if not os.access(path, os.X_OK):
        return ["bash"]
    return []


def _load_spec(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ExecutionError(f"契约文件不是对象: {path}")
    return data


def diff_specs(expected: dict, actual: dict, *, allow_additions: bool = True) -> list[str]:
    """比 paths / operations / parameters / responses。期望是下界，实际不得缺。"""
    problems: list[str] = []
    expected_paths = expected.get("paths") or {}
    actual_paths = actual.get("paths") or {}

    for path, expected_ops in expected_paths.items():
        if path not in actual_paths:
            problems.append(f"缺少路径 {path}")
            continue
        actual_ops = actual_paths[path] or {}
        for method, expected_op in (expected_ops or {}).items():
            if method.lower() not in _HTTP_VERBS:
                continue
            if method.lower() not in {k.lower() for k in actual_ops}:
                problems.append(f"缺少接口 {method.upper()} {path}")
                continue
            actual_op = next(v for k, v in actual_ops.items() if k.lower() == method.lower())
            problems.extend(_diff_operation(path, method, expected_op or {}, actual_op or {}, expected, actual))

    if not allow_additions:
        for path, actual_ops in actual_paths.items():
            if path not in expected_paths:
                problems.append(f"出现未在契约中的路径 {path}")
                continue
            for method in actual_ops:
                if method.lower() not in _HTTP_VERBS:
                    continue
                if method.lower() not in {k.lower() for k in (expected_paths[path] or {})}:
                    problems.append(f"出现未在契约中的接口 {method.upper()} {path}")
    return problems


def _diff_operation(
    path: str, method: str, expected: dict, actual: dict,
    expected_root: dict | None = None, actual_root: dict | None = None,
) -> list[str]:
    problems: list[str] = []
    expected_params = {(p.get("name"), p.get("in")) for p in expected.get("parameters") or []}
    actual_params = {(p.get("name"), p.get("in")) for p in actual.get("parameters") or []}
    for name, location in sorted(expected_params - actual_params, key=lambda x: (str(x[0]), str(x[1]))):
        problems.append(f"{method.upper()} {path} 缺少参数 {name}（in={location}）")

    expected_responses = set((expected.get("responses") or {}).keys())
    actual_responses = set((actual.get("responses") or {}).keys())
    for code in sorted(expected_responses - actual_responses):
        problems.append(f"{method.upper()} {path} 缺少响应码 {code}")

    expected_props = _schema_props(expected, expected_root)
    actual_props = _schema_props(actual, actual_root)
    for name in sorted(expected_props - actual_props):
        problems.append(f"{method.upper()} {path} 响应缺少字段 {name}")

    # requestBody 也是契约的一部分：只声明响应、不校验请求，会让"少传了字段"的
    # 后端实现蒙混过关。期望为空时不校验（保持向后兼容：老契约只写响应）。
    expected_req = _request_props(expected, expected_root)
    actual_req = _request_props(actual, actual_root)
    for name in sorted(expected_req - actual_req):
        problems.append(f"{method.upper()} {path} 请求缺少字段 {name}")
    return problems


def _schema_props(op: dict, root: dict | None = None) -> set[str]:
    props: set[str] = set()
    for response in (op.get("responses") or {}).values():
        content = (response or {}).get("content") or {}
        for media in content.values():
            schema_obj = (media or {}).get("schema") or {}
            # recursive=True：把 data.properties 等嵌套字段也展开。历史原因：
            # 统一响应壳的 data 常被塞进 properties，嵌套字段（facets/lat/lng 等）
            # 只有递归进去才能验到；顶层字段（code/data）仍照常收集。
            _collect_props(schema_obj, props, root, recursive=True)
    return props


def _request_props(op: dict, root: dict | None = None) -> set[str]:
    """收集请求体字段（展开 $ref/allOf 等），供 openapi_diff 比对。"""
    body = op.get("requestBody") or {}
    props: set[str] = set()
    for media in ((body.get("content") or {}).values()):
        _collect_props((media or {}).get("schema") or {}, props, root, recursive=True)
    return props


def _collect_props(
    schema_obj, props: set[str], root: dict | None = None, _seen: frozenset[str] = frozenset(),
    *, recursive: bool = False, _prefix: str = "",
) -> None:
    """收集 schema 字段：properties + allOf/oneOf/anyOf 组合 + 本地 $ref 组件。

    统一响应壳常写成 ``allOf: [$ref ApiResponse, {data: ...}]``，``code`` 等字段
    藏在 ``components.schemas.ApiResponse`` 里；不展开 $ref 会误判「缺少字段 code」。
    只解析文档内本地引用 ``#/...``，不带 root（旧调用）时行为与之前一致。

    ``recursive=True`` 时，嵌套对象的字段以点号路径收集（如 ``data.facets``），
    使 ``data.properties`` 下的真实业务字段也进入比对；默认 False 保持历史行为
    （只收当前层字段名），兼容不带 root 的旧调用。
    """
    if not isinstance(schema_obj, dict):
        return
    for name, sub_schema in (schema_obj.get("properties") or {}).items():
        props.add(f"{_prefix}{name}" if _prefix else name)
        if recursive:
            _collect_props(sub_schema, props, root, _seen, recursive=True, _prefix=f"{_prefix}{name}.")
    for key in ("allOf", "oneOf", "anyOf"):
        for sub in schema_obj.get(key) or []:
            _collect_props(sub, props, root, _seen, recursive=recursive, _prefix=_prefix)
    # items：数组元素的 schema 也要递归，否则 list[].name 这类字段会漏检。
    if recursive and isinstance(schema_obj.get("items"), dict):
        _collect_props(schema_obj["items"], props, root, _seen, recursive=True, _prefix=f"{_prefix}[]")
    ref = schema_obj.get("$ref")
    if not (isinstance(ref, str) and ref.startswith("#/") and root is not None and ref not in _seen):
        return
    target: object = root
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or token not in target:
            target = None
            break
        target = target[token]
    if isinstance(target, dict):
        _collect_props(target, props, root, _seen | {ref}, recursive=recursive, _prefix=_prefix)


def summarize(results: list[CheckResult]) -> str:
    lines = []
    for result in results:
        lines.append(f"[{result.status.upper():<6}] {result.id}  ({result.duration_ms}ms)")
        if result.status != "passed" and result.output:
            for line in result.output.splitlines()[:20]:
                lines.append("    " + line)
    return "\n".join(lines)


def checks_summary_json(results: list[CheckResult]) -> str:
    return json.dumps([r.as_dict() for r in results], ensure_ascii=False)

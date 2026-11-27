"""结构化验收：http / openapi_diff / repo_script，以及部署轮询。"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from handoff import ids
from handoff.codex_runner import build_prompt
from handoff.config import Config
from handoff.deploy import wait_for_deployment
from handoff.http_client import HttpClient
from handoff.store import TaskStore
from handoff.verifier import Verifier, _display_url, _schema_props, _substitute, diff_specs, jsonpath_get
from helpers import git, make_bare_remote, make_store_repo, make_task, repo_entry, write_config

OPENAPI_EXPECTED = {
    "openapi": "3.0.0",
    "paths": {
        "/api/orders": {
            "get": {
                "parameters": [{"name": "page", "in": "query"}],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "properties": {"status": {"type": "string"}}}
                            }
                        }
                    }
                },
            }
        }
    },
}


class _Handler(BaseHTTPRequestHandler):
    spec = dict(OPENAPI_EXPECTED)

    def do_GET(self):  # noqa: N802
        if self.path == "/version":
            self._json({"sha": "c" * 40, "environment": "dev"})
        elif self.path == "/api/orders":
            self._json({"status": "OPEN"})
        elif self.path == "/openapi.json":
            self._json(self.spec)
        else:
            self.send_error(404)

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def http_base():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def build(tmp_path, checks_doc=None, acceptance_checks=None, contract=None, local_doc=None):
    work, _bare = make_store_repo(tmp_path)
    backend = make_bare_remote(tmp_path, "api", {"src/orders/Order.java": "class Order {}\n"})
    api_dir = tmp_path / "api-work"
    (api_dir / "scripts").mkdir(parents=True)
    script = api_dir / "scripts" / "contract-check.sh"
    script.write_text("#!/usr/bin/env bash\necho contract ok\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    default_checks = {
        "test_suites": {},
        "repo_scripts": {"contract-check": {"repo": "org/api", "path": "scripts/contract-check.sh"}},
        "gate": {"high_risk_paths": [".github/**"]},
        "verifier": {"output_limit_chars": 2000, "repo_paths": {"org/api": str(api_dir)}},
    }
    write_config(
        work,
        repos=[repo_entry("org/api", "backend", backend), repo_entry("org/web", "frontend", backend)],
        checks=checks_doc or default_checks,
    )
    if local_doc is not None:
        (work / "config" / "local.yaml").write_text(yaml.safe_dump(local_doc), encoding="utf-8")
    config = Config.load(work)
    store = TaskStore(work, config)
    task = make_task(
        ids.task_id(),
        base_sha=git(backend, "rev-parse", "dev"),
        checks=acceptance_checks or [],
    )
    if contract is not None:
        directory = work / "contracts" / task["id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "openapi.yaml"
        path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    store.create(task)
    return work, config, store, task


def test_jsonpath_get():
    payload = {"a": {"b": [{"c": 1}]}}
    assert jsonpath_get(payload, "$.a.b[0].c") == 1
    assert jsonpath_get(payload, "$['a']['b'][0]['c']") == 1
    with pytest.raises(KeyError):
        jsonpath_get(payload, "$.a.missing")


def test_http_check_passes(tmp_path, http_base):
    checks = [
        {
            "id": "version",
            "type": "http",
            "method": "GET",
            "url": "{base_url}/version",
            "expect_status": 200,
            "expect_jsonpath": [{"path": "$.sha", "matches": "^[0-9a-f]{40}$"}],
        },
        {
            "id": "orders",
            "type": "http",
            "method": "GET",
            "url": "{base_url}/api/orders",
            "expect_status": 200,
            "expect_jsonpath": [{"path": "$.status", "equals": "OPEN"}],
        },
    ]
    work, config, _store, task = build(tmp_path, acceptance_checks=checks)
    verifier = Verifier(work, config, client=HttpClient(timeout=2, allow_private=True))
    results = verifier.run(task, base_url=http_base, observed_sha="c" * 40)
    assert [r.status for r in results] == ["passed", "passed"]


def test_http_check_fails_on_status(tmp_path, http_base):
    checks = [
        {"id": "missing", "type": "http", "method": "GET", "url": "{base_url}/nope", "expect_status": 200}
    ]
    work, config, _store, task = build(tmp_path, acceptance_checks=checks)
    verifier = Verifier(work, config, client=HttpClient(timeout=2, allow_private=True))
    results = verifier.run(task, base_url=http_base)
    assert results[0].status == "failed"
    assert "404" in results[0].output


def test_openapi_diff_passes(tmp_path, http_base):
    contract = {
        "openapi": "3.0.0",
        "paths": {
            "/api/orders": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {"schema": {"type": "object", "properties": {"status": {"type": "string"}}}}
                            }
                        }
                    }
                }
            }
        },
    }
    work, config, _store, task = build(tmp_path, contract=contract)
    task["acceptance"]["checks"] = [
        {
            "id": "contract",
            "type": "openapi_diff",
            "expected_path": f"contracts/{task['id']}/openapi.yaml",
            "actual_url": "{base_url}/openapi.json",
        }
    ]
    verifier = Verifier(work, config, client=HttpClient(timeout=2, allow_private=True))
    results = verifier.run(task, base_url=http_base)
    assert results[0].status == "passed", results[0].output


def test_openapi_diff_detects_missing_operation():
    actual = {"paths": {"/api/orders": {"post": {}}}}
    problems = diff_specs(OPENAPI_EXPECTED, actual)
    assert any("GET /api/orders" in p for p in problems)


def test_openapi_diff_detects_nested_response_field():
    """data.properties 下的嵌套字段必须验到（如 facets/lat/lng）。"""
    expected = {
        "paths": {
            "/api/orders": {
                "post": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "data": {
                                                "type": "object",
                                                "properties": {"facets": {"type": "object"}},
                                            }
                                        },
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    actual = {
        "paths": {
            "/api/orders": {
                "post": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"data": {"type": "object", "properties": {}}},
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    problems = diff_specs(expected, actual)
    assert any("响应缺少字段 data.facets" in p for p in problems), problems


def test_openapi_diff_detects_missing_request_field():
    """requestBody 也是契约：必填请求字段缺失必须报出来。"""
    expected = {
        "paths": {
            "/api/orders": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"orderId": {"type": "integer"}},
                                }
                            }
                        }
                    },
                    "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            }
        }
    }
    actual = {
        "paths": {
            "/api/orders": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "properties": {}}
                            }
                        }
                    },
                    "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            }
        }
    }
    problems = diff_specs(expected, actual)
    assert any("请求缺少字段 orderId" in p for p in problems), problems


def test_schema_props_collects_allof_wrapped_fields():
    """统一响应壳（allOf: [$ref, {properties: {data}}]）里的字段不能漏。"""
    op = {
        "responses": {
            "200": {
                "content": {
                    "application/json": {
                        "schema": {
                            "allOf": [
                                {"$ref": "#/components/schemas/ApiResponse"},
                                {"type": "object", "properties": {"data": {"type": "object"}}},
                            ]
                        }
                    }
                }
            }
        }
    }
    assert _schema_props(op) == {"data"}


def test_env_placeholder_resolved_but_never_displayed(monkeypatch):
    monkeypatch.setenv("DOCS_API_KEY", "secret-123")
    resolved = _substitute("{base_url}/docs/openapi.json?apikey={env:DOCS_API_KEY}", "https://api.example.com")
    assert resolved == "https://api.example.com/docs/openapi.json?apikey=secret-123"
    shown = _display_url("{base_url}/docs/openapi.json?apikey={env:DOCS_API_KEY}", "https://api.example.com")
    assert "secret-123" not in shown
    assert "{env:DOCS_API_KEY}" in shown


def test_env_placeholder_missing_env_is_error(monkeypatch):
    from handoff.errors import ExecutionError

    monkeypatch.delenv("HANDOFF_DEFINITELY_UNSET_KEY", raising=False)
    with pytest.raises(ExecutionError):
        _substitute("{base_url}/x?k={env:HANDOFF_DEFINITELY_UNSET_KEY}", "https://api.example.com")


def test_env_placeholder_falls_back_to_secrets_file(tmp_path, monkeypatch):
    """启动脚本没注入环境变量时，从 secrets/<name>.txt 兜底读取。"""
    from handoff import verifier as vmod

    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "handoff-fallback-key.txt").write_text("file-secret\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HANDOFF_FALLBACK_KEY", raising=False)
    monkeypatch.setattr(vmod, "_SECRET_CACHE", {})

    resolved = _substitute("{base_url}/d?apikey={env:HANDOFF_FALLBACK_KEY}", "https://api.example.com")
    assert resolved == "https://api.example.com/d?apikey=file-secret"
    # 环境变量优先于文件
    monkeypatch.setenv("HANDOFF_FALLBACK_KEY", "env-secret")
    assert _substitute("{env:HANDOFF_FALLBACK_KEY}", "") == "env-secret"


def test_repo_script_runs_without_shell(tmp_path, http_base):
    checks = [{"id": "contract", "type": "repo_script", "repo": "org/api", "name": "contract-check"}]
    work, config, _store, task = build(tmp_path, acceptance_checks=checks)
    verifier = Verifier(work, config, client=HttpClient(timeout=2, allow_private=True))
    results = verifier.run(task, base_url=http_base)
    assert results[0].status == "passed"
    assert "contract ok" in results[0].output


def test_suite_gets_only_declared_secret_and_redacts_output(tmp_path, http_base, monkeypatch):
    secret_path = tmp_path / "suite-secret.txt"
    secret_path.write_text("suite-canary-secret\n", encoding="utf-8")
    monkeypatch.setenv("UNDECLARED_PARENT_CANARY", "must-not-reach-suite")
    checks_doc = {
        "test_suites": {
            "isolated": {
                "repo": "org/api",
                "argv": [
                    sys.executable,
                    "-c",
                    (
                        "import os; "
                        "print(os.environ['SUITE_TOKEN']); "
                        "print(os.environ.get('UNDECLARED_PARENT_CANARY', 'parent-absent'))"
                    ),
                ],
                "required_secrets": [{
                    "ref": "suite-token", "env": "SUITE_TOKEN", "same_value": True
                }],
            }
        },
        "repo_scripts": {},
        "gate": {"high_risk_paths": [".github/**"]},
        "verifier": {"output_limit_chars": 2000, "repo_paths": {"org/api": str(tmp_path / "api-work")}},
    }
    acceptance = [{
        "id": "isolated", "type": "test_suite", "repo": "org/api", "name": "isolated"
    }]
    work, config, _store, task = build(
        tmp_path,
        checks_doc=checks_doc,
        acceptance_checks=acceptance,
        local_doc={"secrets": {"refs": {"suite-token": str(secret_path)}}},
    )
    results = Verifier(work, config, client=HttpClient(timeout=2, allow_private=True)).run(
        task, base_url=http_base
    )
    assert results[0].status == "passed", results[0].output
    assert "suite-canary-secret" not in results[0].output
    assert "[REDACTED_SECRET]" in results[0].output
    assert "parent-absent" in results[0].output
    assert "must-not-reach-suite" not in results[0].output


def test_secret_redaction_happens_before_truncation_and_escaping(tmp_path):
    secret = "sk-boundary-0123456789"
    encoded = "%73%6b%2D%62%6F%75%6E%64%61%72%79%2D%30%31%32%33%34%35%36%37%38%39"
    secret_path = tmp_path / "suite-secret.txt"
    secret_path.write_text(secret, encoding="utf-8")
    # Emit the secret first (inside the kept head), then again after > limit chars
    # (inside the kept tail), plus percent/JSON escaped forms.
    script = (
        "import os,json; "
        "v=os.environ['SUITE_TOKEN']; "
        f"print('head-'+v); "
        f"print('x'*3000); "
        f"print('tail-'+v); "
        f"print('pct-'+__import__('urllib.parse',fromlist=['quote']).quote(v,safe='')); "
        f"print('json-'+json.dumps(v)[1:-1])"
    )
    checks_doc = {
        "test_suites": {
            "isolated": {
                "repo": "org/api",
                "argv": [sys.executable, "-c", script],
                "required_secrets": [{
                    "ref": "suite-token", "env": "SUITE_TOKEN", "same_value": True
                }],
            }
        },
        "repo_scripts": {},
        "verifier": {"output_limit_chars": 2000, "repo_paths": {"org/api": str(tmp_path / "api-work")}},
    }
    acceptance = [{
        "id": "isolated", "type": "test_suite", "repo": "org/api", "name": "isolated",
    }]
    work, config, _store, task = build(
        tmp_path,
        checks_doc=checks_doc,
        acceptance_checks=acceptance,
        local_doc={"secrets": {"refs": {"suite-token": str(secret_path)}}},
    )
    results = Verifier(work, config).run(task, base_url="http://127.0.0.1:1")
    output = results[0].output
    assert secret not in output
    assert encoded not in output
    assert "REDACTED_SECRET" in output
    assert len(output) <= 2100


def test_unregistered_script_is_rejected_before_creation(tmp_path):
    from handoff.errors import SchemaError

    checks = [{"id": "x", "type": "repo_script", "repo": "org/api", "name": "nope"}]
    with pytest.raises(SchemaError):
        build(tmp_path, acceptance_checks=checks)


def test_prompt_contains_scope_and_untrusted_warning(tmp_path):
    work, _config, _store, task = build(tmp_path)
    prompt = build_prompt(task, "/tmp/wt", "openapi: 3.0.0")
    assert "src/orders/**" in prompt
    assert "不可信数据" in prompt
    assert "openapi: 3.0.0" in prompt
    assert "不要执行 git commit" in prompt


class _FakeProbe:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = 0

    def fetch(self, env):
        self.calls += 1
        return self.sequence[min(self.calls - 1, len(self.sequence) - 1)]


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _env():
    return type("E", (), {"name": "dev", "version_url": lambda self: "http://x/version"})()


def _poll(timeout):
    return type(
        "P",
        (),
        {
            "interval_seconds": 1,
            "timeout_seconds": timeout,
            "request_timeout_seconds": 1,
            "backoff_factor": 1,
            "max_interval_seconds": 1,
        },
    )()


def test_wait_for_deployment_exact_match():
    clock = _Clock()
    probe = _FakeProbe([(None, "boom"), ("a" * 40, None), ("c" * 40, None)])
    result = wait_for_deployment(
        _env(), "c" * 40, _poll(10), probe=probe, sleep=clock.sleep, clock=clock
    )
    assert result.status == "deployed"
    assert result.observed_sha == "c" * 40
    assert probe.calls == 3


def test_wait_for_deployment_accepts_descendant():
    clock = _Clock()
    target = "c" * 40
    observed = "d" * 40
    probe = _FakeProbe([(observed, None)])
    compared = []

    result = wait_for_deployment(
        _env(),
        target,
        _poll(10),
        probe=probe,
        sleep=clock.sleep,
        clock=clock,
        contains=lambda base, head: (compared.append((base, head)) or True, None),
    )

    assert result.status == "deployed"
    assert result.observed_sha == observed
    assert compared == [(target, observed)]


def test_wait_for_deployment_timeout_keeps_observed():
    clock = _Clock()
    probe = _FakeProbe([("d" * 40, None)])
    result = wait_for_deployment(
        _env(), "c" * 40, _poll(5), probe=probe, sleep=clock.sleep, clock=clock
    )
    assert result.status == "timeout"
    assert result.observed_sha == "d" * 40

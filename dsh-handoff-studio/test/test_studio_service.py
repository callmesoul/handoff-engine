"""StudioService 的行为测试：执行器表、模型目录、路由发现。

不起 HTTP 服务，只在进程内构造服务对象；用一个假交接仓把 Config 真实加载起来，
确保继承关系与父类契约都是真的（而不是 mock 出来的）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

def _repo_root() -> Path:
    """交接仓根：优先 HANDOFF_ROOT，否则往上找 config/repositories.yaml。"""
    override = os.environ.get("HANDOFF_ROOT", "").strip()
    if override:
        return Path(override)
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "config" / "repositories.yaml").is_file():
            return candidate
    return Path(__file__).resolve().parents[2]


REPO_ROOT = _repo_root()
ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"
# handoff 包从引擎仓的 src/ 加载；交接仓只提供 config/ 与 schemas/。
ENGINE_SRC = Path(__file__).resolve().parents[1].parent / "src"
for extra in (str(ENGINE_SRC), str(ENGINE_DIR)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from handoff.config import Config  # noqa: E402
from handoff.notification import load_secret  # noqa: E402
from handoff.panel import service as panel_service  # noqa: E402
import handoff_studio.service as studio  # noqa: E402
from handoff_studio import secrets_bridge  # noqa: E402


@pytest.fixture()
def store_root(tmp_path: Path) -> Path:
    """把交接仓的 config/ 与 schemas/ 拷到一个临时目录，得到可加载的根。"""
    root = tmp_path / "store"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    # schemas 是 handoff 包的内部资源（src/handoff/schemas），仓根不一定有副本；
    # 只在旧布局里存在时才拷，避免测试绑死目录结构。
    if (REPO_ROOT / "schemas").is_dir():
        shutil.copytree(REPO_ROOT / "schemas", root / "schemas")
    (root / "tasks").mkdir()
    (root / "events").mkdir()
    (root / "contracts").mkdir()
    return root


@pytest.fixture()
def service(store_root: Path, monkeypatch, tmp_path):
    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://127.0.0.1:59999/handoff-studio")
    config = Config.load(store_root)
    return studio.StudioService(store_root, config)


def test_studio_uses_its_own_state_file(service):
    """不能与原面板共用 panel-state.json：那边的 auto.executor 是 codex。"""
    assert service.state_path.name == "studio-state.json"
    state = service.state()
    assert state["state_file"].endswith("studio-state.json")
    # 原面板的 auto.executor=codex 没有泄漏进来
    assert state["auto"]["executor"] == "dsh"


def test_shared_panel_state_does_not_leak_into_studio(store_root, monkeypatch):
    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://127.0.0.1:59999/handoff-studio")
    # 模拟原面板留下的状态文件
    panel_state = store_root / ".handoff-run" / "panel-state.json"
    panel_state.parent.mkdir(parents=True, exist_ok=True)
    panel_state.write_text(
        json.dumps(
            {
                "mode": "auto",
                "auto": {
                    "executor": "codex",
                    "model": "gpt-6.1-sol",
                    "effort": "medium",
                    "work_dir": "D:/project/api",
                },
            }
        ),
        encoding="utf-8",
    )
    config = Config.load(store_root)
    service = studio.StudioService(store_root, config)
    state = service.state()
    assert state["auto"]["executor"] == "dsh"
    assert state["auto"].get("work_dir") is None
    # 也没有去改动原面板那份文件
    untouched = json.loads(panel_state.read_text(encoding="utf-8"))
    assert untouched["auto"]["executor"] == "codex"


def test_studio_writes_do_not_touch_the_original_state(store_root, monkeypatch):
    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://127.0.0.1:59999/handoff-studio")
    panel_state = store_root / ".handoff-run" / "panel-state.json"
    panel_state.parent.mkdir(parents=True, exist_ok=True)
    original = {"mode": "auto", "auto": {"executor": "codex", "auto_merge": True}}
    panel_state.write_text(json.dumps(original), encoding="utf-8")

    service = studio.StudioService(store_root, Config.load(store_root))
    service.select_model({"model": "m", "provider": "p"})

    assert json.loads(panel_state.read_text(encoding="utf-8")) == original
    assert (store_root / ".handoff-run" / "studio-state.json").is_file()
    assert studio.EXECUTORS == ("dsh", "codex", "zcode")
    assert service._default_executor() == "dsh"
    state = service.state()
    assert state["default_executor"] == "dsh"
    assert set(state["executors"]) == {"dsh", "codex", "zcode"}
    assert state["executors_order"] == ["dsh", "codex", "zcode"]


def test_parent_module_table_is_widened_in_this_process(service):
    # 父类的校验读的是模块级常量；这里必须是拓宽后的版本，否则 run_task 会拒 dsh
    assert panel_service.EXECUTORS == ("dsh", "codex", "zcode")


def test_unknown_executor_is_rejected(service):
    with pytest.raises(Exception) as exc:
        service._ensure_executor("nope")
    assert "未知执行器" in str(exc.value)


def test_route_discovery_uses_env(service):
    assert service._require_route() == "http://127.0.0.1:59999/handoff-studio"


def test_route_discovery_rejects_non_loopback(monkeypatch):
    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://10.0.0.5:1/handoff-studio")
    with pytest.raises(Exception) as exc:
        studio.discover_route()
    assert "loopback" in str(exc.value)


def test_catalog_remembers_models_and_efforts(service):
    catalog = {
        "default": {"provider": "workbuddy", "model": "deepseek-v4.1-flash", "reasoningEffort": "high"},
        "groups": [
            {
                "id": "workbuddy",
                "name": "WorkBuddy",
                "models": [
                    {
                        "id": "deepseek-v4.1-flash",
                        "name": "DeepSeek-V4.1-Flash",
                        "reasoning": {"efforts": [{"id": "low"}, {"id": "high"}], "defaultEffort": "high"},
                    }
                ],
            }
        ],
    }
    remembered = service.remember_catalog(catalog)
    assert remembered["model"] == "deepseek-v4.1-flash"
    assert remembered["provider"] == "workbuddy"
    assert remembered["effort"] == "high"
    assert remembered["models"][0]["id"] == "deepseek-v4.1-flash"
    assert remembered["models"][0]["provider_name"] == "WorkBuddy"
    assert remembered["efforts"] == ["low", "high"]

    options = service._executor_options()
    assert options["dsh"]["catalog"] == "live"
    assert options["dsh"]["models"][0]["id"] == "deepseek-v4.1-flash"
    # 原有执行器的选项完全没被动
    assert set(options) == {"dsh", "codex", "zcode"}


def test_select_model_switches_effort_set(service):
    service.remember_catalog(
        {
            "groups": [
                {
                    "id": "p",
                    "name": "P",
                    "models": [
                        {"id": "a", "reasoning": {"efforts": [{"id": "low"}]}},
                        {"id": "b", "reasoning": {"efforts": [{"id": "max"}]}},
                    ],
                }
            ]
        }
    )
    picked = service.select_model({"model": "b", "effort": "max"})
    assert picked["model"] == "b"
    assert picked["effort"] == "max"
    assert picked["efforts"] == ["max"]

    with pytest.raises(Exception) as exc:
        service.select_model({"model": "a", "effort": "max"})
    assert "不支持思考程度" in str(exc.value)


def test_selection_survives_a_restart(store_root, monkeypatch):
    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://127.0.0.1:59999/handoff-studio")
    config = Config.load(store_root)
    first = studio.StudioService(store_root, config)
    first.remember_catalog(
        {"groups": [{"id": "p", "name": "P", "models": [{"id": "m1", "reasoning": {"efforts": [{"id": "low"}]}}]}]}
    )
    first.select_model({"model": "m1", "provider": "p", "effort": "low"})

    second = studio.StudioService(store_root, config)
    state = second.studio_models()
    assert state["model"] == "m1"
    assert state["provider"] == "p"
    assert state["effort"] == "low"
    # 目录也一起持久化了，重启后下拉不会空
    assert state["models"][0]["id"] == "m1"


def test_missing_route_does_not_block_panel_startup(store_root, monkeypatch):
    """插件没装时面板仍要能用：列表/详情能开，只是执行会报错。"""
    monkeypatch.delenv("HANDOFF_STUDIO_URL", raising=False)
    monkeypatch.setenv("DSH_HOME", str(store_root / "no-such-home"))
    monkeypatch.delenv("DSH_WEB_URL", raising=False)
    config = Config.load(store_root)
    service = studio.StudioService(store_root, config)
    health = service.state()["executors"]["dsh"]
    assert health["ok"] is False
    assert "找不到 DSH Studio 地址" in health["error"]
    with pytest.raises(Exception) as exc:
        service._require_route()
    assert "找不到 DSH Studio 地址" in str(exc.value)


def test_sessions_rejects_unknown_executor(service):
    with pytest.raises(Exception) as exc:
        service.sessions("nope")
    assert "未知执行器" in str(exc.value)
    # dsh 没有历史会话时返回空表，不抛错
    assert service.sessions("dsh") == []


# -- 候选模型链 --------------------------------------------------------------
# 实机需求：主模型额度用完时，自动模式要能往下换候选模型，而不是把任务退回队列
# 等人发现。链存在 auto.fallback_models 里，跟其它自动配置放一起。

CATALOG = {
    "groups": [
        {
            "id": "p1",
            "name": "Provider One",
            "models": [
                {"id": "m1", "reasoning": {"efforts": [{"id": "low"}, {"id": "high"}]}},
                {"id": "m2", "reasoning": {"efforts": [{"id": "max"}]}},
            ],
        },
        {
            "id": "p2",
            "name": "Provider Two",
            "models": [
                # 同名模型在另一个 provider 下档位不同——这正是要按 provider 对齐的原因
                {"id": "m2", "reasoning": {"efforts": [{"id": "xhigh"}]}},
            ],
        },
    ]
}


def test_fallback_chain_defaults_to_empty(service):
    assert service.state()["auto"]["fallback_models"] == []
    # 空链不能报错，也不能被当成「配置有误」
    assert service.set_mode({"mode": "manual", "auto": {"fallback_models": []}})["auto"]["fallback_models"] == []


def test_set_mode_stores_the_fallback_chain(service):
    service.remember_catalog(CATALOG)
    state = service.set_mode(
        {
            "mode": "auto",
            "auto": {
                "executor": "dsh",
                "model": "m1",
                "fallback_models": [
                    {"provider": "p1", "model": "m1", "effort": "high"},
                    {"provider": "p2", "model": "m2", "effort": "xhigh"},
                ],
            },
        }
    )
    assert state["auto"]["fallback_models"] == [
        {"provider": "p1", "model": "m1", "effort": "high"},
        {"provider": "p2", "model": "m2", "effort": "xhigh"},
    ]
    # 顺序即优先级：读回来必须一样
    assert [item["model"] for item in service._fallback_chain()] == ["m1", "m2"]


def test_set_mode_rejects_a_fallback_without_a_model(service):
    with pytest.raises(Exception) as exc:
        service.set_mode({"mode": "manual", "auto": {"fallback_models": [{"provider": "p1"}]}})
    assert "fallback_models" in str(exc.value)


def test_set_mode_rejects_a_non_list_fallback_chain(service):
    with pytest.raises(Exception) as exc:
        service.set_mode({"mode": "manual", "auto": {"fallback_models": "m1"}})
    assert "fallback_models" in str(exc.value)


def test_a_rejected_fallback_chain_leaves_the_old_one_intact(service):
    """校验失败不能留下半套配置：先校验后落盘。"""
    service.remember_catalog(CATALOG)
    service.set_mode({"mode": "manual", "auto": {"fallback_models": [{"model": "m1"}]}})
    with pytest.raises(Exception):
        service.set_mode({"mode": "manual", "auto": {"fallback_models": [{"provider": "p1"}]}})
    assert service.state()["auto"]["fallback_models"] == [{"provider": None, "model": "m1", "effort": None}]


def test_fallback_chain_drops_models_that_left_the_catalog(service):
    service.remember_catalog(CATALOG)
    service.set_mode(
        {
            "mode": "manual",
            "auto": {"fallback_models": [{"model": "m1"}, {"model": "retired-model"}]},
        }
    )
    # 保存时照收（面板允许保留一条「不在目录」的候选给人自己决定），
    # 但真正构造链时只留下还能路由的
    assert len(service.state()["auto"]["fallback_models"]) == 2
    assert [item["model"] for item in service._fallback_chain()] == ["m1"]


def test_fallback_chain_resolves_the_provider_from_the_catalog(service):
    service.remember_catalog(CATALOG)
    service.set_mode({"mode": "manual", "auto": {"fallback_models": [{"model": "m1"}]}})
    chain = service._fallback_chain()
    assert chain[0]["provider"] == "p1", "a bare model must be pinned to its provider"


def test_fallback_chain_clears_an_effort_the_model_does_not_declare(service):
    service.remember_catalog(CATALOG)
    service.set_mode(
        {
            "mode": "manual",
            "auto": {"fallback_models": [{"provider": "p2", "model": "m2", "effort": "max"}]},
        }
    )
    # p2 的 m2 只声明 xhigh：把 max 留着会在派单时被宿主拒掉
    assert service._fallback_chain()[0]["effort"] is None


def test_fallback_chain_is_deduped(service):
    service.remember_catalog(CATALOG)
    service.set_mode(
        {
            "mode": "manual",
            "auto": {
                "fallback_models": [
                    {"provider": "p1", "model": "m1"},
                    {"provider": "p1", "model": "m1", "effort": "high"},
                ]
            },
        }
    )
    # 同一条重复只会让额度耗尽时白试一轮
    assert len(service._fallback_chain()) == 1


def test_fallback_chain_survives_a_restart(store_root, monkeypatch):
    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://127.0.0.1:59999/handoff-studio")
    config = Config.load(store_root)
    first = studio.StudioService(store_root, config)
    first.remember_catalog(CATALOG)
    first.set_mode({"mode": "manual", "auto": {"fallback_models": [{"provider": "p2", "model": "m2"}]}})

    second = studio.StudioService(store_root, config)
    assert second.state()["auto"]["fallback_models"] == [
        {"provider": "p2", "model": "m2", "effort": None}
    ]
    assert [item["model"] for item in second._fallback_chain()] == ["m2"]


def test_a_hand_broken_fallback_entry_does_not_break_startup(store_root, monkeypatch):
    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://127.0.0.1:59999/handoff-studio")
    state_path = store_root / ".handoff-run" / "studio-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "mode": "manual",
                "auto": {
                    "executor": "dsh",
                    "fallback_models": ["nope", {"provider": "p1"}, {"model": "  m1  ", "effort": "  "}],
                },
            }
        ),
        encoding="utf-8",
    )
    service = studio.StudioService(store_root, Config.load(store_root))
    # 坏条目被丢掉，好条目被清洗后留下——不是整份状态作废
    assert service.state()["auto"]["fallback_models"] == [
        {"provider": None, "model": "m1", "effort": None}
    ]


def test_run_options_expose_the_fallback_chain(service):
    service.remember_catalog(CATALOG)
    service.set_mode({"mode": "manual", "auto": {"fallback_models": [{"provider": "p1", "model": "m1"}]}})
    options = service.run_options()
    dsh = options["executors"][0]
    assert dsh["fallbacks"] == [
        {
            "provider": "p1",
            "model": "m1",
            "effort": None,
            "available": True,
            "efforts": ["low", "high"],
        }
    ]


def test_studio_models_expose_the_fallback_chain(service):
    service.remember_catalog(CATALOG)
    service.set_mode({"mode": "manual", "auto": {"fallback_models": [{"provider": "p1", "model": "m1"}]}})
    assert service.studio_models()["fallbacks"][0]["model"] == "m1"


def test_the_runner_is_built_with_the_fallback_chain(service, monkeypatch):
    """真正的接线点：构造 DshAgentRunner 时必须把链带上，否则一次都不换。"""
    service.remember_catalog(CATALOG)
    service.set_mode(
        {
            "mode": "manual",
            "auto": {
                "executor": "dsh",
                "model": "m1",
                "fallback_models": [{"provider": "p2", "model": "m2", "effort": "xhigh"}],
            },
        }
    )
    service._pending_provider = "p1"
    # 自动模式会把这些作为 run_task 参数传下来（父类 _auto_loop 就是这么做的）
    runner = service._build_runner("dsh", model="m1", effort=None)
    assert runner.model == "m1"
    assert runner.provider == "p1"
    assert runner.model_chain() == [
        {"provider": "p1", "model": "m1", "effort": None},
        {"provider": "p2", "model": "m2", "effort": "xhigh"},
    ]


def test_the_runner_uses_the_configured_sandbox_when_the_request_omits_it(service, monkeypatch):
    """配置里选的访问权限必须真的生效，不能只在请求里带才生效。

    真机踩到的坑：自动配置里写的是「完全权限」，但面板的「执行」按钮只发
    executor，`run_task` 又只认请求参数，于是每一轮都退回部署默认的
    workspace-write——每个新建 worktree 都要现场补 ACL，补不上就是
    SANDBOX_PREPARATION_FAILED，agent 的每个 shell 工具全部失败。
    """
    service.remember_catalog(CATALOG)
    service.set_mode(
        {
            "mode": "manual",
            "auto": {"executor": "dsh", "model": "m1", "sandbox": "danger-full-access"},
        }
    )
    captured = {}

    def fake_run_task(self_, task_id, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(panel_service.PanelService, "run_task", fake_run_task)
    service.run_task("ho-test", executor="dsh")

    assert captured.get("sandbox") == "danger-full-access"


def test_an_explicit_sandbox_in_the_request_wins_over_the_config(service, monkeypatch):
    """请求显式给了档位就按请求走：那是这一轮的临时选择。"""
    service.remember_catalog(CATALOG)
    service.set_mode(
        {"mode": "manual", "auto": {"executor": "dsh", "model": "m1", "sandbox": "danger-full-access"}}
    )
    captured = {}

    def fake_run_task(self_, task_id, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(panel_service.PanelService, "run_task", fake_run_task)
    service.run_task("ho-test", executor="dsh", sandbox="read-only")

    assert captured.get("sandbox") == "read-only"


def test_an_unknown_sandbox_value_falls_back_to_the_config(service, monkeypatch):
    """拼错的档位不能原样派给宿主，也不能把配置里的有效值一起弄丢。"""
    service.remember_catalog(CATALOG)
    service.set_mode(
        {"mode": "manual", "auto": {"executor": "dsh", "model": "m1", "sandbox": "workspace-write"}}
    )
    captured = {}

    def fake_run_task(self_, task_id, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(panel_service.PanelService, "run_task", fake_run_task)
    service.run_task("ho-test", executor="dsh", sandbox="totally-wrong")

    assert captured.get("sandbox") == "workspace-write"


def test_token_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HANDOFF_STUDIO_TOKEN", "abc")
    assert studio.studio_token() == "abc"


def test_token_file_is_read_when_env_is_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("HANDOFF_STUDIO_TOKEN", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    (home / studio.TOKEN_FILENAME).write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(home))
    assert studio.studio_token() == "from-file"


def test_token_file_missing_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.delenv("HANDOFF_STUDIO_TOKEN", raising=False)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "empty"))
    with pytest.raises(Exception) as exc:
        studio.studio_token()
    assert "读取 DSH Studio 令牌失败" in str(exc.value)


def test_endpoint_file_is_preferred_over_web_url(store_root, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / studio.ENDPOINT_FILENAME).write_text(
        json.dumps({"version": studio.ENDPOINT_VERSION, "url": "http://127.0.0.1:51234/handoff-studio"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DSH_HOME", str(home))
    monkeypatch.delenv("HANDOFF_STUDIO_URL", raising=False)
    monkeypatch.setenv("DSH_WEB_URL", "http://127.0.0.1:1")
    route, endpoint = studio.discover_route()
    assert route == "http://127.0.0.1:51234/handoff-studio"
    assert endpoint["url"].endswith("/handoff-studio")


def test_endpoint_with_wrong_version_falls_back(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / studio.ENDPOINT_FILENAME).write_text(
        json.dumps({"version": 99, "url": "http://127.0.0.1:1/handoff-studio"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DSH_HOME", str(home))
    monkeypatch.delenv("HANDOFF_STUDIO_URL", raising=False)
    monkeypatch.setenv("DSH_WEB_URL", "http://127.0.0.1:2")
    route, _ = studio.discover_route()
    assert route == "http://127.0.0.1:2/handoff-studio"


# -- 通知密钥发现 ------------------------------------------------------------
# 实机 bug：studio 引擎是 DSH 插件 spawn 的，不继承 run-panel.ps1 注入的
# HANDOFF_NOTIFY_SECRET，于是每次用到通知都报「密钥缺失或少于 32 字节」——
# 密钥其实好好躺在 secrets/notify-secret.txt 里。这组测试锁住按文件发现这条路。

LONG_SECRET = "a1" * 32  # 64 个十六进制字符 = 32 字节随机值的十六进制编码


def _clear_secret_env(monkeypatch):
    for name in (
        "HANDOFF_NOTIFY_SECRET",
        "HANDOFF_NOTIFY_SECRET_FILE",
        "HANDOFF_STUDIO_NOTIFY_SECRET_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_notify_secret_found_next_to_store(store_root: Path, monkeypatch):
    """本机布局：store-github + 上一级 secrets/notify-secret.txt。"""
    _clear_secret_env(monkeypatch)
    secrets = store_root.parent / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "notify-secret.txt").write_text(LONG_SECRET + "\n", encoding="utf-8")

    status = secrets_bridge.discover_notify_secret(store_root)
    assert status.ok is True
    assert status.length == len(LONG_SECRET)
    assert os.environ["HANDOFF_NOTIFY_SECRET"] == LONG_SECRET
    # 状态里绝不能出现密钥本身
    assert LONG_SECRET not in json.dumps(status.as_dict())


def test_short_secret_is_reported_not_injected(store_root: Path, monkeypatch):
    """短密钥是配置错误：不能静默降级成「能用」。"""
    _clear_secret_env(monkeypatch)
    secrets = store_root / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "notify-secret.txt").write_text("too-short", encoding="utf-8")
    status = secrets_bridge.discover_notify_secret(store_root)
    assert status.ok is False
    assert "少于" in (status.error or "")
    assert "HANDOFF_NOTIFY_SECRET" not in os.environ


def test_service_seeds_environment_before_parent_starts(store_root: Path, monkeypatch):
    """服务构造完就该能用：父类唤醒监听第一件事就是读密钥。"""
    _clear_secret_env(monkeypatch)
    secrets = store_root.parent / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "notify-secret.txt").write_text(LONG_SECRET, encoding="utf-8")
    monkeypatch.setenv("HANDOFF_STUDIO_URL", "http://127.0.0.1:59999/handoff-studio")
    service = studio.StudioService(store_root, Config.load(store_root))

    state = service.state()
    assert state["notify_secret"]["ok"] is True
    assert state["notify_secret"]["length"] == len(LONG_SECRET)
    # app 自己的 load_secret 现在能过（原始报错发生的地方）
    config = Config.load(store_root)
    assert load_secret(config.notifications) == LONG_SECRET.encode("utf-8")

"""StudioService 的行为测试：执行器表、模型目录、路由发现。

不起 HTTP 服务，只在进程内构造服务对象；用一个假交接仓把 Config 真实加载起来，
确保继承关系与父类契约都是真的（而不是 mock 出来的）。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"
for extra in (str(REPO_ROOT / "src"), str(ENGINE_DIR)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from handoff.config import Config  # noqa: E402
from handoff.panel import service as panel_service  # noqa: E402
import handoff_studio.service as studio  # noqa: E402


@pytest.fixture()
def store_root(tmp_path: Path) -> Path:
    """把交接仓的 config/ 与 schemas/ 拷到一个临时目录，得到可加载的根。"""
    root = tmp_path / "store"
    root.mkdir()
    for name in ("config", "schemas"):
        shutil.copytree(REPO_ROOT / name, root / name)
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

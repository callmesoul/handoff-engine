from __future__ import annotations

import pytest

from handoff.config import RepoConfig, RepoScopeConfig, ScopeProfileConfig
from handoff.errors import SchemaError
from handoff.scope import profile_options, resolve_scope


def configured_repo() -> RepoConfig:
    return RepoConfig(
        id="org/api",
        role="backend",
        scope=RepoScopeConfig(
            default_profile="application",
            forbidden_paths=(".github/**", "**/auth/**"),
            profiles={
                "application": ScopeProfileConfig(
                    allowed_paths=("src/**", "tests/**"),
                    forbidden_paths=("**/auth/**", "infra/prod/**"),
                ),
                "docs": ScopeProfileConfig(allowed_paths=("docs/**",)),
            },
        ),
    )


def test_resolve_scope_uses_default_and_merges_forbidden_paths():
    resolved = resolve_scope(configured_repo(), out_of_scope=["不改数据库"])
    assert resolved == {
        "out_of_scope": ["不改数据库"],
        "allowed_paths": ["src/**", "tests/**"],
        "forbidden_paths": [".github/**", "**/auth/**", "infra/prod/**"],
    }


def test_resolve_scope_selects_semantic_profile():
    resolved = resolve_scope(configured_repo(), profile_name="docs")
    assert resolved["allowed_paths"] == ["docs/**"]
    assert resolved["forbidden_paths"] == [".github/**", "**/auth/**"]


def test_resolve_scope_requires_repository_template():
    repo = RepoConfig(id="org/api", role="backend")
    with pytest.raises(SchemaError, match="未配置 scope 模板"):
        resolve_scope(repo)


def test_resolve_scope_rejects_unknown_profile():
    with pytest.raises(SchemaError, match="missing"):
        resolve_scope(configured_repo(), profile_name="missing")


def test_profile_options_are_read_only_resolved_views():
    options = profile_options(configured_repo())
    assert options[0]["default"] is True
    assert options[0]["forbidden_paths"] == [".github/**", "**/auth/**", "infra/prod/**"]
    assert options[1]["name"] == "docs"

"""把仓库拥有者维护的 scope profile 解析成任务中的固定路径边界。"""

from __future__ import annotations

from .config import RepoConfig
from .errors import SchemaError


def _dedupe(values) -> list[str]:
    return list(dict.fromkeys(values))


def resolve_scope(
    repo: RepoConfig,
    *,
    profile_name: str | None = None,
    out_of_scope: list[str] | None = None,
) -> dict:
    """解析并固化任务 scope；缺模板时 fail closed，不回退到 ``**``。"""
    scope = repo.scope
    if scope is None:
        raise SchemaError(
            f"目标项目 {repo.id!r} 未配置 scope 模板；"
            "请由仓库拥有者在 config/repositories.yaml 中配置 scope"
        )

    if profile_name is not None and not isinstance(profile_name, str):
        raise SchemaError("scope_profile 必须是字符串")
    selected = (profile_name or scope.default_profile).strip()
    profile = scope.profiles.get(selected)
    if profile is None:
        choices = ", ".join(scope.profiles)
        raise SchemaError(
            f"目标项目 {repo.id!r} 没有 scope profile {selected!r}（可选: {choices}）"
        )

    if out_of_scope is not None and not isinstance(out_of_scope, list):
        raise SchemaError("scope.out_of_scope 必须是字符串数组")

    return {
        "out_of_scope": list(out_of_scope or []),
        "allowed_paths": list(profile.allowed_paths),
        "forbidden_paths": _dedupe((*scope.forbidden_paths, *profile.forbidden_paths)),
    }


def profile_options(repo: RepoConfig) -> list[dict]:
    """返回给面板的语义 profile 及其最终路径，只读展示。"""
    scope = repo.scope
    if scope is None:
        return []
    return [
        {
            "name": name,
            "default": name == scope.default_profile,
            "allowed_paths": list(profile.allowed_paths),
            "forbidden_paths": _dedupe((*scope.forbidden_paths, *profile.forbidden_paths)),
        }
        for name, profile in scope.profiles.items()
    ]

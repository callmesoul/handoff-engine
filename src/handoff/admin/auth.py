"""后台鉴权。角色只决定能做什么，最终仍需 TaskStore 做状态校验。"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config, UserConfig
from ..errors import HandoffError


class AuthError(HandoffError):
    code = "unauthorized"
    exit_code = 11


ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}


@dataclass(frozen=True)
class Principal:
    id: str
    role: str
    repos: tuple[str, ...]

    def can(self, action: str) -> bool:
        required = {"read": "viewer", "operate": "operator", "admin": "admin"}[action]
        return ROLE_RANK[self.role] >= ROLE_RANK[required]

    def sees(self, repo: str | None) -> bool:
        if not repo:
            return True
        return "*" in self.repos or repo in self.repos


def authenticate(config: Config, token: str | None) -> Principal:
    if not token:
        raise AuthError("缺少访问令牌")
    for user in config.admin.users:
        if _constant_time_eq(user.token, token):
            return Principal(id=user.id, role=user.role, repos=user.repos)
    raise AuthError("访问令牌无效")


def _constant_time_eq(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def users_by_role(config: Config, role: str) -> list[UserConfig]:
    return [u for u in config.admin.users if u.role == role]

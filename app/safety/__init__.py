"""Safety 模块：压测目标白名单 + 资源硬上限 + 确认机制。

原则：
1. 默认只允许 localhost / 显式授权域名。
2. 用户数、时长、RPS 有硬上限，超过需显式确认。
3. 仅对拥有授权的系统进行压测。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import get_settings


class SafetyError(Exception):
    """安全策略拦截。"""


@dataclass
class SafetyCheck:
    """一次安全检查的结果。"""

    ok: bool
    reason: str = ""
    target_host: str = ""
    require_confirmation: bool = False


def _extract_host(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname or ""
    # 去掉端口，保留 hostname
    return host.lower()


def check_target(url: str, *, explicit_allow: bool = False) -> SafetyCheck:
    """校验目标 URL 是否在白名单内。

    Args:
        url: 目标地址。
        explicit_allow: 是否已获得用户显式授权（绕过白名单，但仍需走上限检查）。
    """
    settings = get_settings()
    host = _extract_host(url)
    if not host:
        return SafetyCheck(ok=False, reason="无法解析目标 URL 的主机名")

    allowed = host in settings.allowed_hosts or host in {"localhost", "127.0.0.1", "::1"}
    if not allowed and not explicit_allow:
        return SafetyCheck(
            ok=False,
            reason=f"目标主机 '{host}' 不在白名单中。默认仅允许 localhost，"
            f"或通过环境变量 AP_ALLOWED_HOSTS 显式授权。",
            target_host=host,
        )
    return SafetyCheck(ok=True, target_host=host)


def check_limits(
    *,
    users: int,
    duration_seconds: int,
    rps: int | None = None,
    confirmed: bool = False,
) -> SafetyCheck:
    """校验并发/时长/RPS 是否超过硬上限。超过且未确认则要求确认。"""
    settings = get_settings()
    problems: list[str] = []

    if users > settings.max_users:
        problems.append(f"用户数 {users} 超过硬上限 {settings.max_users}")
    if duration_seconds > settings.max_duration_seconds:
        problems.append(f"时长 {duration_seconds}s 超过硬上限 {settings.max_duration_seconds}s")
    if rps is not None and rps > settings.max_rps:
        problems.append(f"RPS {rps} 超过硬上限 {settings.max_rps}")

    if not problems:
        return SafetyCheck(ok=True)

    reason = "; ".join(problems)
    if confirmed:
        # 已显式确认，放行（但记录）
        return SafetyCheck(ok=True, reason=reason, require_confirmation=False)
    return SafetyCheck(ok=False, reason=reason, require_confirmation=True)


def validate_request(url: str, users: int, duration_seconds: int, rps: int | None = None) -> None:
    """一次完成目标与上限校验，不通过抛 SafetyError。"""
    target_check = check_target(url)
    if not target_check.ok:
        raise SafetyError(target_check.reason)

    limit_check = check_limits(users=users, duration_seconds=duration_seconds, rps=rps)
    if not limit_check.ok:
        raise SafetyError(limit_check.reason)

"""OpenAI-compatible endpoint identity and credential authority.

The URL is configuration, not a credential selector by substring.  This module
is deliberately pure: it never reads settings or secrets and never performs
network I/O.  Factory, router and API/core validation share this one decision.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal
from urllib.parse import SplitResult, urlsplit


CredentialAuthority = Literal["dashscope", "deepseek", "tokenhub", "generic"]


@dataclass(frozen=True, slots=True)
class OpenAICompatibleEndpoint:
    """Validated endpoint identity plus the only settings field allowed for it."""

    hostname: str
    port: int | None
    credential_authority: CredentialAuthority
    settings_field: str


_OFFICIAL_HOSTS: dict[str, tuple[CredentialAuthority, str]] = {
    "dashscope.aliyuncs.com": ("dashscope", "dashscope_api_key"),
    "api.deepseek.com": ("deepseek", "deepseek_api_key"),
    "tokenhub.tencentmaas.com": ("tokenhub", "tokenhub_api_key"),
}
_ALIBABA_BEIJING_WORKSPACE_HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cn-beijing\.maas\.aliyuncs\.com$"
)


def _parse_base_url(base_url: str) -> SplitResult:
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("openai_compatible base_url 必须是非空 URL")
    if base_url != base_url.strip() or any(char.isspace() for char in base_url):
        raise ValueError("openai_compatible base_url 不允许包含空白字符")
    try:
        parsed = urlsplit(base_url)
    except ValueError as exc:
        raise ValueError("openai_compatible base_url 不是合法 URL") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("openai_compatible base_url 必须使用 https")
    if not parsed.netloc or parsed.hostname is None:
        raise ValueError("openai_compatible base_url 必须包含合法 hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("openai_compatible base_url 不允许包含 userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("openai_compatible base_url 不允许包含 query 或 fragment")
    return parsed


def _canonical_hostname(parsed: SplitResult) -> str:
    raw_hostname = parsed.hostname
    if raw_hostname is None:
        raise ValueError("openai_compatible base_url 必须包含合法 hostname")
    raw_hostname = raw_hostname.rstrip(".").lower()
    if not raw_hostname:
        raise ValueError("openai_compatible base_url 必须包含合法 hostname")
    try:
        hostname = raw_hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("openai_compatible base_url hostname 非法") from exc
    if any(not label for label in hostname.split(".")):
        raise ValueError("openai_compatible base_url hostname 非法")
    return hostname


def resolve_openai_compatible_endpoint(
    base_url: str,
    *,
    alibaba_model_studio_workspace_id: str | None = None,
) -> OpenAICompatibleEndpoint:
    """Validate ``base_url`` and resolve its exact credential authority.

    Dedicated secrets are available only to the exact official HTTPS origins
    or the one Alibaba Beijing hostname derived from the configured workspace
    ID (implicit/default 443 or explicit 443).  Every other valid HTTPS
    hostname is a generic compatible endpoint and may receive only the generic
    secret.  A dedicated hostname on a non-standard port is rejected instead
    of silently changing authority.
    """

    parsed = _parse_base_url(base_url)
    hostname = _canonical_hostname(parsed)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("openai_compatible base_url port 非法") from exc

    official = _OFFICIAL_HOSTS.get(hostname)
    if official is not None:
        if port not in (None, 443):
            raise ValueError(
                "openai_compatible base_url 官方 hostname 只允许 HTTPS 默认端口 443"
            )
        authority, settings_field = official
        return OpenAICompatibleEndpoint(
            hostname=hostname,
            port=port,
            credential_authority=authority,
            settings_field=settings_field,
        )

    # Alibaba workspace-specific Beijing endpoint.  The full hostname must
    # equal the one derived from the configured non-secret workspace ID; merely
    # matching the Beijing suffix (including another workspace or ``trial``)
    # remains generic and cannot receive the DashScope credential.
    expected_workspace_host = (
        f"{alibaba_model_studio_workspace_id}.cn-beijing.maas.aliyuncs.com"
        if alibaba_model_studio_workspace_id
        else None
    )
    if (
        _ALIBABA_BEIJING_WORKSPACE_HOST_RE.fullmatch(hostname)
        and hostname == expected_workspace_host
    ):
        if port not in (None, 443):
            raise ValueError(
                "Alibaba 北京 workspace hostname 只允许 HTTPS 默认端口 443"
            )
        return OpenAICompatibleEndpoint(
            hostname=hostname,
            port=port,
            credential_authority="dashscope",
            settings_field="dashscope_api_key",
        )

    return OpenAICompatibleEndpoint(
        hostname=hostname,
        port=port,
        credential_authority="generic",
        settings_field="openai_compatible_api_key",
    )


__all__ = [
    "CredentialAuthority",
    "OpenAICompatibleEndpoint",
    "resolve_openai_compatible_endpoint",
]

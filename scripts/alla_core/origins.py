"""Project-owned exact origins for all credential-bearing TestOps requests."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from alla_core.exceptions import ConfigurationError


def origin(url: str, *, origin_only: bool = False) -> str:
    """Canonical scheme, IDNA host and effective port; reject ambiguous authorities."""
    try:
        if not isinstance(url, str) or not url or any(ord(c) <= 32 for c in url) or "\\" in url:
            raise ValueError
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError
        host = parts.hostname
        if not host or parts.username is not None or parts.password is not None \
                or host.endswith(".") or "%" in host or parts.fragment:
            raise ValueError
        if origin_only and (parts.path not in ("", "/") or parts.query):
            raise ValueError
        port = parts.port
        if port is None:
            port = 443 if parts.scheme == "https" else 80
        if not 1 <= port <= 65535 or parts.netloc.endswith(":"):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # httpx uses IDNA 2008: use the same encoding as the actual transport.
            host = httpx.URL(url).raw_host.decode("ascii").lower()
            if re.fullmatch(r"[0-9.]+", host) or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            ):
                raise ValueError from None
        else:
            host = address.compressed
            if address.version == 6:
                host = f"[{host}]"
        return f"{parts.scheme}://{host}:{port}"
    except (ValueError, TypeError, UnicodeError, httpx.InvalidURL):
        raise ConfigurationError("TestOps: нужен корректный http:// или https:// URL без userinfo, "
                                 "fragment и неоднозначного имени хоста") from None


def load_allowed_origins(project_root: Path | None) -> tuple[str, ...]:
    """Never take policy from environment or the skill's distributable configuration."""
    if project_root is None:
        return ()
    config = project_root / "alla-extensions" / "config.toml"
    if not config.exists():
        return ()
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError("Не удалось прочитать alla-extensions/config.toml") from exc
    table = data.get("testops", {})
    values = table.get("allowed_origins", []) if isinstance(table, dict) else None
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ConfigurationError("[testops].allowed_origins должен быть списком точных origins")
    return tuple(dict.fromkeys(origin(value, origin_only=True) for value in values))


def require_allowed_origin(url: str, allowed_origins: tuple[str, ...]) -> None:
    requested = origin(url)
    policy = {origin(item, origin_only=True) for item in allowed_origins}
    if not policy or requested not in policy:
        raise ConfigurationError("Адрес TestOps запрещён: добавьте точный origin в "
                                 "[testops].allowed_origins файла alla-extensions/config.toml")

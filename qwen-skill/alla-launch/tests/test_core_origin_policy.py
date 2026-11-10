"""Exact project policy is enforced before TestOps credentials reach any transport."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import skill_fixtures
from alla_core.clients.auth import AllureAuthManager
from alla_core.clients.mcp_client import TestOpsMcpClient
from alla_core.clients.testops_client import AllureTestOpsClient
from alla_core.config import Settings
from alla_core.exceptions import AllureApiError, AuthenticationError, ConfigurationError
from alla_core.origins import load_allowed_origins, origin, require_allowed_origin


@pytest.mark.parametrize(("url", "expected"), [
    ("HTTPS://EXAMPLE.COM/path", "https://example.com:443"),
    ("https://example.com:443/", "https://example.com:443"),
    ("http://127.0.0.1:8080/a", "http://127.0.0.1:8080"),
    ("https://[2001:0db8:0:0:0:0:0:1]/a", "https://[2001:db8::1]:443"),
    ("https://пример.рф/a", "https://xn--e1afmkfd.xn--p1ai:443"),
])
def test_origin_normalization(url: str, expected: str) -> None:
    assert origin(url) == expected


@pytest.mark.parametrize("url", [
    "https://allowed.example.", "https://user@allowed.example", "https://user:p@allowed.example",
    "https://allowed.example:0", "https://allowed.example:", "https://allowed.example:99999",
    "https://allowed.example/#fragment", "https://allowed.example\\@evil.example",
    " https://allowed.example", "https://allowed.example\n", "file:///tmp/testops",
    "https://127.1", "https://0177.0.0.1", "https://[fe80::1%25en0]", "https://%61llowed.example",
])
def test_ambiguous_or_credential_bearing_urls_are_rejected(url: str) -> None:
    with pytest.raises(ConfigurationError):
        origin(url)


def test_policy_is_exact_not_suffix_or_scheme_or_port() -> None:
    allowed = ("https://allowed.example",)
    require_allowed_origin("https://ALLOWED.EXAMPLE:443/api/mcp", allowed)
    for denied in ("http://allowed.example", "https://allowed.example:444",
                   "https://allowed.example.evil", "https://sub.allowed.example"):
        with pytest.raises(ConfigurationError, match="запрещён"):
            require_allowed_origin(denied, allowed)


@pytest.mark.parametrize("contents", ["", "[testops]\n", "[testops]\nallowed_origins=[]\n"])
def test_missing_or_empty_policy_denies(tmp_path: Path, contents: str) -> None:
    config = tmp_path / "alla-extensions" / "config.toml"
    config.parent.mkdir()
    config.write_text(contents)
    with pytest.raises(ConfigurationError, match="allowed_origins"):
        Settings.load(project_root=tmp_path, environ={"ALLURE_ENDPOINT": "https://allowed.example",
                                                    "ALLURE_TOKEN": "secret"})


@pytest.mark.parametrize("contents", [
    "[testops]\nallowed_origins='https://allowed.example'", "[testops]\nallowed_origins=[1]",
    "testops=[]", "[testops]\nallowed_origins=['https://allowed.example/api']",
    "[testops]\nallowed_origins=['https://allowed.example/?x=1']", "not toml {",
])
def test_malformed_policy_fails_closed(tmp_path: Path, contents: str) -> None:
    config = tmp_path / "alla-extensions" / "config.toml"
    config.parent.mkdir()
    config.write_text(contents)
    with pytest.raises(ConfigurationError):
        load_allowed_origins(tmp_path)


def test_environment_cannot_expand_project_policy(tmp_path: Path) -> None:
    skill_fixtures.allow_testops(tmp_path, "https://allowed.example")
    base = {"ALLURE_ENDPOINT": "https://allowed.example", "ALLURE_TOKEN": "secret",
            "ALLURE_ALLOWED_ORIGINS": "https://evil.example"}
    settings = Settings.load(project_root=tmp_path, environ=base)
    assert settings.allowed_origins == ("https://allowed.example:443",)
    with pytest.raises(ConfigurationError, match="запрещён"):
        Settings.load(project_root=tmp_path, environ={**base, "ALLURE_ENDPOINT": "https://evil.example"})
    with pytest.raises(ConfigurationError, match="запрещён"):
        Settings.load(project_root=tmp_path, environ={**base, "ALLURE_MCP_URL": "https://evil.example/mcp"})


def test_local_settings_do_not_require_or_read_testops_policy(tmp_path: Path) -> None:
    config = tmp_path / "alla-extensions" / "config.toml"
    config.parent.mkdir()
    config.write_text("invalid {")
    assert Settings.load(project_root=tmp_path, environ={}, require_testops=False).endpoint == ""


@pytest.mark.parametrize("factory", [
    lambda: TestOpsMcpClient(Settings(endpoint="https://evil.example", token="secret")),
    lambda: AllureTestOpsClient(Settings(endpoint="https://evil.example", token="secret"), AsyncMock()),
    lambda: AllureAuthManager("https://evil.example", "secret"),
    lambda: TestOpsMcpClient(Settings(endpoint="https://allowed.example", token="secret",
                                    mcp_url="https://evil.example/mcp",
                                    allowed_origins=("https://allowed.example",))),
])
def test_direct_construction_fails_before_http_client(monkeypatch: pytest.MonkeyPatch, factory) -> None:
    def unexpected_http(*args, **kwargs):
        pytest.fail("policy must reject before constructing a transport")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_http)
    with pytest.raises(ConfigurationError):
        factory()


@pytest.mark.parametrize("kind", ["mcp", "auth", "resolve"])
def test_redirect_is_an_error_and_never_forwards_credentials(kind: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/collect"})

    async def run() -> None:
        settings = Settings(endpoint="https://allowed.example", token="canary-secret",
                            allowed_origins=("https://allowed.example",))
        transport = httpx.MockTransport(handler)
        if kind == "auth":
            auth = AllureAuthManager(settings.endpoint, settings.token,
                                    allowed_origins=settings.allowed_origins)
            await auth._http.aclose()
            auth._http = httpx.AsyncClient(transport=transport, follow_redirects=False)
            try:
                with pytest.raises(AuthenticationError, match="перенаправление"):
                    await auth.get_auth_header()
            finally:
                await auth.close()
        elif kind == "mcp":
            client = TestOpsMcpClient(settings)
            await client._http.aclose()
            client._http = httpx.AsyncClient(transport=transport, follow_redirects=False)
            try:
                with pytest.raises(AllureApiError, match="перенаправление"):
                    await client.connect()
            finally:
                await client.close()
        else:
            auth = AsyncMock()
            auth.get_auth_header.return_value = {"Authorization": "Bearer canary-secret"}
            rest = AllureTestOpsClient(settings, auth)
            await rest._http.aclose()
            rest._http = httpx.AsyncClient(transport=transport, follow_redirects=False)
            try:
                with pytest.raises(AllureApiError, match="перенаправление"):
                    await rest.resolve_test_result(1, "passed", "checked")
            finally:
                await rest.close()

    asyncio.run(run())
    assert len(calls) == 1
    assert calls[0].url.host == "allowed.example"

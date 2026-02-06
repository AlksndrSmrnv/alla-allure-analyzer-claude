"""Allure TestOps JWT authentication manager."""

from __future__ import annotations

import logging
import time

import httpx

from alla.exceptions import AuthenticationError

logger = logging.getLogger(__name__)


class _TokenInfo:
    """Cached JWT token with expiry tracking."""

    __slots__ = ("access_token", "obtained_at", "expires_in")

    def __init__(self, access_token: str, expires_in: int = 3600) -> None:
        self.access_token = access_token
        self.obtained_at = time.time()
        self.expires_in = expires_in

    @property
    def is_expired(self) -> bool:
        """True if the token expires within the next 5 minutes."""
        return time.time() > (self.obtained_at + self.expires_in - 300)


class AllureAuthManager:
    """Manages Allure TestOps JWT authentication.

    Exchanges an API token for a JWT via ``POST /api/uaa/oauth/token``,
    caches the JWT, and refreshes it before expiry.
    """

    TOKEN_ENDPOINT = "/api/uaa/oauth/token"

    def __init__(
        self,
        endpoint: str,
        api_token: str,
        *,
        timeout: int = 30,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_token = api_token
        self._token_info: _TokenInfo | None = None
        self._http = httpx.AsyncClient(timeout=timeout)

    async def get_auth_header(self) -> dict[str, str]:
        """Return the ``Authorization`` header with a valid JWT.

        Exchanges the API token for a JWT if no cached token exists
        or the cached token is about to expire.
        """
        if self._token_info is None or self._token_info.is_expired:
            self._token_info = await self._exchange_token()
        return {"Authorization": f"Bearer {self._token_info.access_token}"}

    async def _exchange_token(self) -> _TokenInfo:
        """Exchange API token for JWT via the OAuth endpoint."""
        url = f"{self._endpoint}{self.TOKEN_ENDPOINT}"
        logger.debug("Exchanging API token for JWT at %s", url)

        try:
            resp = await self._http.post(
                url,
                data={
                    "grant_type": "apitoken",
                    "scope": "openid",
                    "token": self._api_token,
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AuthenticationError(
                f"JWT exchange failed: HTTP {exc.response.status_code} from {url}"
            ) from exc
        except httpx.RequestError as exc:
            raise AuthenticationError(
                f"JWT exchange request failed: {exc}"
            ) from exc

        body = resp.json()
        access_token = body.get("access_token")
        if not access_token:
            raise AuthenticationError(
                f"JWT exchange response missing 'access_token': {body}"
            )

        expires_in = int(body.get("expires_in", 3600))
        logger.debug("JWT obtained, expires in %d seconds", expires_in)
        return _TokenInfo(access_token=access_token, expires_in=expires_in)

    def invalidate(self) -> None:
        """Force re-authentication on the next request."""
        self._token_info = None

    async def close(self) -> None:
        """Release HTTP client resources."""
        await self._http.aclose()

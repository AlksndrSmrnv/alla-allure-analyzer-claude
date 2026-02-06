"""Менеджер JWT-аутентификации Allure TestOps."""

from __future__ import annotations

import logging
import time

import httpx

from alla.exceptions import AuthenticationError

logger = logging.getLogger(__name__)


class _TokenInfo:
    """Кэшированный JWT-токен с отслеживанием срока действия."""

    __slots__ = ("access_token", "obtained_at", "expires_in")

    def __init__(self, access_token: str, expires_in: int = 3600) -> None:
        self.access_token = access_token
        self.obtained_at = time.time()
        self.expires_in = expires_in

    @property
    def is_expired(self) -> bool:
        """True, если токен истекает в ближайшие 5 минут."""
        return time.time() > (self.obtained_at + self.expires_in - 300)


class AllureAuthManager:
    """Управление JWT-аутентификацией Allure TestOps.

    Обменивает API-токен на JWT через ``POST /api/uaa/oauth/token``,
    кэширует JWT и обновляет его до истечения срока действия.
    """

    TOKEN_ENDPOINT = "/api/uaa/oauth/token"

    def __init__(
        self,
        endpoint: str,
        api_token: str,
        *,
        timeout: int = 30,
        ssl_verify: bool = True,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_token = api_token
        self._token_info: _TokenInfo | None = None
        self._http = httpx.AsyncClient(timeout=timeout, verify=ssl_verify)

    async def get_auth_header(self) -> dict[str, str]:
        """Вернуть заголовок ``Authorization`` с валидным JWT.

        Обменивает API-токен на JWT, если кэшированного токена нет
        или кэшированный токен скоро истекает.
        """
        if self._token_info is None or self._token_info.is_expired:
            self._token_info = await self._exchange_token()
        return {"Authorization": f"Bearer {self._token_info.access_token}"}

    async def _exchange_token(self) -> _TokenInfo:
        """Обменять API-токен на JWT через OAuth-эндпоинт."""
        url = f"{self._endpoint}{self.TOKEN_ENDPOINT}"
        logger.debug("Обмен API-токена на JWT по адресу %s", url)

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
                f"Не удалось обменять API-токен на JWT: HTTP "
                f"{exc.response.status_code} от {url}"
            ) from exc
        except httpx.RequestError as exc:
            raise AuthenticationError(
                f"Ошибка запроса при обмене API-токена на JWT: {exc}"
            ) from exc

        try:
            body = resp.json()
        except Exception as exc:
            raise AuthenticationError(
                f"Ответ аутентификации не является валидным JSON: {resp.text[:200]}"
            ) from exc
        access_token = body.get("access_token")
        if not access_token:
            raise AuthenticationError(
                f"В ответе обмена JWT отсутствует 'access_token': {body}"
            )

        expires_in = int(body.get("expires_in", 3600))
        logger.debug("JWT получен, срок действия %d секунд", expires_in)
        return _TokenInfo(access_token=access_token, expires_in=expires_in)

    def invalidate(self) -> None:
        """Принудительная повторная аутентификация при следующем запросе."""
        self._token_info = None

    async def close(self) -> None:
        """Освободить ресурсы HTTP-клиента."""
        await self._http.aclose()

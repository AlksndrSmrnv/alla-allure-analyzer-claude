"""HTTP-клиент для Langflow REST API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from alla.exceptions import LangflowApiError

logger = logging.getLogger(__name__)


class LangflowClient:
    """HTTP-клиент для взаимодействия с Langflow REST API.

    Отправляет текстовые запросы в указанный flow и получает
    текстовые ответы от LLM.
    """

    def __init__(
        self,
        base_url: str,
        flow_id: str,
        api_key: str,
        *,
        timeout: int = 120,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._flow_id = flow_id
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=timeout)

    async def run_flow(self, input_value: str) -> str:
        """Отправить текст в Langflow flow и вернуть текстовый ответ.

        POST {base_url}/api/v1/run/{flow_id}
        Body: {"input_value": "...", "output_type": "chat", "input_type": "chat"}
        Auth: Bearer token

        Returns:
            Текстовый ответ LLM.

        Raises:
            LangflowApiError: При HTTP-ошибках или неожиданном формате ответа.
        """
        url = f"{self._base_url}/api/v1/run/{self._flow_id}"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload = {
            "input_value": input_value,
            "output_type": "chat",
            "input_type": "chat",
        }

        logger.debug(
            "Langflow запрос: POST %s input_length=%d",
            url,
            len(input_value),
        )

        try:
            resp = await self._http.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LangflowApiError(
                0,
                f"Таймаут запроса: {exc}",
                url,
            ) from exc
        except httpx.RequestError as exc:
            raise LangflowApiError(0, str(exc), url) from exc

        if resp.status_code >= 400:
            body_text = resp.text[:500]
            raise LangflowApiError(resp.status_code, body_text, url)

        try:
            data = resp.json()
        except Exception as exc:
            raise LangflowApiError(
                resp.status_code,
                f"Ответ не является валидным JSON: {resp.text[:200]}",
                url,
            ) from exc

        return self._extract_text(data, url)

    @staticmethod
    def _extract_text(data: dict[str, Any], url: str) -> str:
        """Извлечь текст ответа из стандартного Langflow JSON.

        Путь: outputs[0].outputs[0].results.message.text
        """
        try:
            outputs = data["outputs"][0]["outputs"][0]
            text = outputs["results"]["message"]["text"]
            if not isinstance(text, str):
                msg = f"Ожидался str, получен {type(text).__name__}"
                raise TypeError(msg)
            return text
        except (KeyError, IndexError, TypeError) as exc:
            raise LangflowApiError(
                0,
                f"Неожиданная структура ответа Langflow: {exc}. "
                f"Ответ: {str(data)[:300]}",
                url,
            ) from exc

    async def close(self) -> None:
        """Освободить ресурсы HTTP-клиента."""
        await self._http.aclose()

    async def __aenter__(self) -> LangflowClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

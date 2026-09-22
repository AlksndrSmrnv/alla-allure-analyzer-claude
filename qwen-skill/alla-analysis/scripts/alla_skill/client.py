"""Read-only TestOps client with bounded downloads and evidence provenance."""

import asyncio
import time
from pathlib import Path

import httpx

from .config import Settings
from .evidence import redact, private_write, decode_attachment
from .models.testops import AttachmentMeta, ExecutionStep, LaunchResponse, TestResultResponse


class Client:
    def __init__(self, settings: Settings, *, transport=None):
        self.settings = settings
        self.http = httpx.AsyncClient(timeout=settings.request_timeout, transport=transport)
        self.sources = {}
        self.attachment_mimes = {}
        self.artifacts: Path | None = None
        self.launch = None
        self.hidden_count = 0
        self.secrets = [settings.token]
        self._jwt = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def _auth(self):
        async with self._lock:
            if self._jwt and time.monotonic() < self._expires:
                return self._jwt
            try:
                response = await self.http.post(
                    self.settings.endpoint.rstrip("/") + "/api/uaa/oauth/token",
                    data={
                        "grant_type": "apitoken",
                        "scope": "openid",
                        "token": self.settings.token,
                    },
                )
                response.raise_for_status()
                body = response.json()
                token = body["access_token"]
                if not isinstance(token, str) or not token:
                    raise ValueError("missing token")
                expires = int(body.get("expires_in", 3600))
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                detail = (
                    f"HTTP {exc.response.status_code}"
                    if isinstance(exc, httpx.HTTPStatusError)
                    else type(exc).__name__
                )
                raise ValueError(
                    f"Не удалось авторизоваться в TestOps ({detail}); проверьте endpoint и token"
                ) from exc
            self._jwt = token
            self.secrets.append(token)
            self._expires = time.monotonic() + max(1, expires - 30)
            return token

    async def _get(self, path, *, params=None, max_bytes=None):
        auth_refreshed = False
        for attempt in range(self.settings.retries + 2):
            token = await self._auth()
            delay = min(2**attempt, 8)
            try:
                async with self.http.stream(
                    "GET",
                    self.settings.endpoint.rstrip("/") + path,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if response.status_code == 401 and not auth_refreshed:
                        async with self._lock:
                            if self._jwt == token:
                                self._expires = 0
                        auth_refreshed = True
                        continue
                    if (
                        response.status_code in (429, 502, 503, 504)
                        and attempt < self.settings.retries
                    ):
                        try:
                            delay = min(
                                30, max(0, float(response.headers.get("Retry-After", delay)))
                            )
                        except ValueError:
                            pass
                    elif response.is_error:
                        raise ValueError(f"TestOps HTTP {response.status_code}: {path}")
                    else:
                        chunks, total, capped = [], 0, False
                        async for chunk in response.aiter_bytes():
                            if max_bytes is not None and total + len(chunk) > max_bytes:
                                chunks.append(chunk[: max_bytes - total])
                                capped = True
                                break
                            chunks.append(chunk)
                            total += len(chunk)
                        return b"".join(chunks), capped
            except httpx.RequestError:
                if attempt >= self.settings.retries:
                    raise ValueError(f"Сетевая ошибка TestOps: {path}") from None
            await asyncio.sleep(delay)
        raise ValueError(f"Исчерпаны попытки TestOps: {path}")

    async def _json(self, path, params=None):
        import json

        raw, capped = await self._get(path, params=params, max_bytes=32 * 1024 * 1024)
        if capped:
            raise ValueError(f"Ответ API превышает лимит: {path}")
        try:
            return redact(json.loads(raw), self.secrets)
        except (ValueError, UnicodeError):
            raise ValueError(f"Некорректный JSON TestOps: {path}") from None

    async def _pages(self, path, params):
        collected, seen = [], set()
        expected = None
        for page in range(self.settings.max_pages):
            data = await self._json(path, {**params, "page": page, "size": self.settings.page_size})
            if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                raise ValueError(f"Некорректная страница TestOps: {path}")
            total = data.get("totalElements")
            if total is not None:
                if expected is not None and total != expected:
                    raise ValueError("Результаты изменились при пагинации; повторите подготовку")
                expected = total
            items = data["content"]
            for item in items:
                item_id = item.get("id")
                if not isinstance(item_id, int) or item_id in seen:
                    raise ValueError(f"Пропущенный или повторный ID в странице: {path}")
                seen.add(item_id)
                collected.append(item)
            pages = data.get("totalPages")
            finished = data.get("last") is True or (pages is not None and page + 1 >= pages)
            if expected is not None and len(collected) >= expected:
                finished = True
            if expected is None and pages is None and "last" not in data:
                finished = len(items) < self.settings.page_size
            if finished:
                if expected is not None and len(collected) != expected:
                    raise ValueError(f"Неполные данные TestOps: {path}")
                return collected
            if not items:
                raise ValueError(f"Пустая промежуточная страница TestOps: {path}")
        raise ValueError(f"Достигнут лимит страниц TestOps: {path}")

    async def _source(self, key, operation):
        try:
            result = await operation()
        except Exception as exc:
            reason = redact(str(exc), self.secrets)[:500]
            self.sources[key] = {"state": "unavailable", "reason": reason}
            raise ValueError(f"Источник {key} недоступен или некорректен: {reason}") from exc
        self.sources[key] = {"state": "received" if result else "absent"}
        return result

    async def get_launch(self, launch_id):
        async def load():
            value = LaunchResponse.model_validate(await self._json(f"/api/launch/{launch_id}"))
            if value.id != launch_id:
                raise ValueError("TestOps вернул другой launch ID")
            return value

        self.launch = await self._source(f"launch:{launch_id}", load)
        return self.launch

    async def get_all_test_results_for_launch(self, launch_id):
        async def load():
            return [
                TestResultResponse.model_validate(x)
                for x in await self._pages("/api/testresult", {"launchId": launch_id})
            ]

        results = await self._source(f"results:{launch_id}", load)
        self.hidden_count = sum(r.hidden for r in results)
        active = sum(
            not r.hidden and not r.muted and (r.status or "").lower() in ("failed", "broken")
            for r in results
        )
        if active > 1000:
            raise ValueError(
                "Первая версия поддерживает до 1000 активных падений; данные не усечены, анализ остановлен"
            )
        return results

    async def get_test_result_detail(self, result_id):
        async def load():
            return TestResultResponse.model_validate(
                await self._json(f"/api/testresult/{result_id}")
            )

        return await self._source(f"detail:{result_id}", load)

    async def get_test_result_execution(self, result_id):
        async def load():
            data = await self._json(f"/api/testresult/{result_id}/execution")
            if isinstance(data, list):
                return [ExecutionStep.model_validate(x) for x in data]
            if isinstance(data, dict):
                if any(data.get(k) for k in ("steps", "message", "trace", "statusDetails")):
                    return [ExecutionStep.model_validate(data)]
                return [ExecutionStep.model_validate(x) for x in data.get("content", [])]
            raise ValueError("Некорректное дерево выполнения")

        return await self._source(f"execution:{result_id}", load)

    async def get_attachments_for_test_result(self, result_id):
        async def load():
            return [
                AttachmentMeta.model_validate(x)
                for x in await self._pages(
                    "/api/testresult/attachment", {"testResultId": result_id}
                )
            ]

        attachments = await self._source(f"attachments:{result_id}", load)
        for att in attachments:
            self.attachment_mimes[att.id] = (
                (att.content_type or att.type or "").lower().split(";")[0]
            )
            self.sources.setdefault(
                f"attachment:{att.id}",
                {
                    "state": "skipped",
                    "reason": "Не загружено: неподдерживаемый формат",
                    "test_result_ids": [],
                },
            )
            self.sources[f"attachment:{att.id}"]["test_result_ids"].append(result_id)
        return attachments

    async def get_attachment_content(self, attachment_id):
        key = f"attachment:{attachment_id}"
        owners = self.sources.get(key, {}).get("test_result_ids", [])
        try:
            raw, capped = await self._get(
                f"/api/testresult/attachment/{attachment_id}/content",
                max_bytes=self.settings.max_attachment_bytes,
            )
            mime = self.attachment_mimes.get(attachment_id, "")
            declared_text = mime.startswith("text/") or mime in {
                "application/json",
                "application/xml",
                "application/x-ndjson",
            }
            text = decode_attachment(raw, declared_text=declared_text)
            if text is None:
                self.sources[key] = {
                    "state": "skipped",
                    "reason": "Бинарное или нераспознанное текстовое содержимое",
                    "test_result_ids": owners,
                }
                return b""
        except Exception:
            self.sources[key] = {"state": "unavailable", "test_result_ids": owners}
            raise
        text = redact(text, self.secrets)
        self.sources[key] = {
            "state": "truncated" if capped else ("received" if text else "absent"),
            "downloaded_bytes": len(raw),
            "test_result_ids": owners,
        }
        if self.artifacts is not None and text:
            path = self.artifacts / f"{attachment_id}.txt"
            private_write(path, text)
            self.sources[key]["file"] = f"attachments/{attachment_id}.txt"
        return text.encode("utf-8")

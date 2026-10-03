"""Запись прогона TestOps в кассету — для команды, локально.

    python tests/eval/record.py <launch_id> <out_dir>

Сбор — обычный ``pipeline.collect_launch`` скилла с настройками из ``<skill>/.env`` и
окружения ``ALLURE_*``; ответы TestOps по пути раскладываются в кассету
(``eval/cassette.py``). Заголовки запросов, обмен токена и его ответ (JWT) на диск не
попадают. Каталог кассеты — данные команды: держите его вне git и никому не передавайте.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

if __package__ in (None, ""):  # запуск файлом: tests/ и scripts/ в sys.path
    _TESTS = Path(__file__).resolve().parents[1]
    for _path in (_TESTS, _TESTS.parent / "scripts"):
        if str(_path) not in sys.path:
            sys.path.insert(0, str(_path))

import httpx  # noqa: E402

TOKEN_PATH = "/api/uaa/oauth/token"
_LAUNCH_RE = re.compile(r"^/api/launch/(\d+)$")
_DETAIL_RE = re.compile(r"^/api/testresult/(\d+)$")
_EXECUTION_RE = re.compile(r"^/api/testresult/(\d+)/execution$")
_CONTENT_RE = re.compile(r"^/api/testresult/attachment/(\d+)/content$")
# Тело уже раскодировано: длину и сжатие пересчитает httpx.Response.
_DROPPED_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


@dataclass
class Recording:
    """Ответы TestOps, разложенные по частям кассеты."""

    launch: dict[str, Any] | None = None
    pages: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    executions: dict[int, Any] = field(default_factory=dict)
    details: dict[int, Any] = field(default_factory=dict)
    attachments: dict[int, Any] = field(default_factory=dict)
    contents: dict[int, bytes] = field(default_factory=dict)
    # Путь TestOps за прокси: ALLURE_ENDPOINT=https://host/testops → «/testops».
    prefix: str = ""

    def api_path(self, request: httpx.Request) -> str:
        """Путь запроса от корня TestOps (``/api/...``), без префикса из ALLURE_ENDPOINT."""
        path = request.url.path
        if self.prefix and path.startswith(self.prefix + "/"):
            return path[len(self.prefix):]
        return path

    def store(self, request: httpx.Request, body: bytes, response: httpx.Response) -> None:
        path = self.api_path(request)
        if path == TOKEN_PATH or response.status_code >= 400:
            return
        if match := _CONTENT_RE.match(path):
            self.contents[int(match[1])] = body
            return
        try:
            data = httpx.Response(200, content=body).json() if body else None
        except ValueError:
            return  # не JSON — не из тех ответов, что нужны кассете
        if match := _LAUNCH_RE.match(path):
            self.launch = data
        elif path == "/api/testresult" and isinstance(data, dict):
            self.pages[int(request.url.params.get("page", "0"))] = list(data.get("content", []))
        elif path == "/api/testresult/attachment" and isinstance(data, dict):
            result_id = int(request.url.params["testResultId"])
            self.attachments[result_id] = list(data.get("content", []))
        elif match := _EXECUTION_RE.match(path):
            self.executions[int(match[1])] = data
        elif match := _DETAIL_RE.match(path):
            self.details[int(match[1])] = data

    def fixture(self) -> Any:
        from skill_fake_testops import LaunchFixture

        if self.launch is None:
            raise RuntimeError("Ответ GET /api/launch/{id} не записан — прогон не получен.")
        return LaunchFixture(
            launch=self.launch,
            results=[item for page in sorted(self.pages) for item in self.pages[page]],
            executions=self.executions,
            details=self.details,
            attachments=self.attachments,
            contents=self.contents,
        )


class RecordingTransport(httpx.AsyncBaseTransport):
    """Транспорт, который передаёт запросы дальше и складывает ответы в ``Recording``."""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, recording: Recording, max_content_bytes: int
    ) -> None:
        self._inner = inner
        self._recording = recording
        self._max_content_bytes = max_content_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if _CONTENT_RE.match(self._recording.api_path(request)):
            # Больше лимита клиент всё равно не читает: +1 байт, чтобы он увидел обрезку.
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > self._max_content_bytes:
                    del buffer[self._max_content_bytes + 1:]
                    break
            await response.aclose()
            body = bytes(buffer)
        else:
            body = await response.aread()
        self._recording.store(request, body, response)
        headers = [
            (name, value) for name, value in response.headers.items()
            if name.lower() not in _DROPPED_HEADERS
        ]
        return httpx.Response(response.status_code, headers=headers, content=body,
                              request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()


@contextmanager
def recording_clients(
    recording: Recording,
    max_content_bytes: int,
    inner: Callable[[bool], httpx.AsyncBaseTransport] | None = None,
) -> Generator[None]:
    """Подменить ``httpx.AsyncClient`` клиентом с записывающим транспортом.

    ``inner(verify)`` строит настоящий транспорт; по умолчанию — ``AsyncHTTPTransport``.
    """
    real_client = httpx.AsyncClient
    make_inner = inner or (lambda verify: httpx.AsyncHTTPTransport(verify=verify))

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = RecordingTransport(
                make_inner(kwargs.get("verify", True)), recording, max_content_bytes
            )
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = _Client  # type: ignore[misc]
    try:
        yield
    finally:
        httpx.AsyncClient = real_client  # type: ignore[misc]


def record(
    launch_id: int,
    out_dir: Path,
    settings: Any,
    inner: Callable[[bool], httpx.AsyncBaseTransport] | None = None,
) -> Any:
    """Получить прогон обычным сбором скилла и сохранить кассету; вернуть ``LaunchData``."""
    from alla_skill_lib.pipeline import collect_launch

    from eval.cassette import save_cassette

    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"Каталог кассеты {out_dir} не пуст: укажите новый каталог.")
    recording = Recording(prefix=urlsplit(settings.endpoint).path.rstrip("/"))
    with recording_clients(recording, settings.logs_max_attachment_bytes, inner):
        data = asyncio.run(collect_launch(launch_id, settings))
    save_cassette(recording.fixture(), out_dir)
    return data


def main(argv: list[str] | None = None) -> int:
    import argparse

    from alla_core.config import Settings
    from alla_skill_lib import workspace

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("launch_id", type=int)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv)
    settings = Settings.load(env_file=workspace.SKILL_DIR / ".env")
    try:
        data = record(args.launch_id, args.out_dir, settings)
    except FileExistsError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(
        f"Кассета записана: {args.out_dir} — результатов {data.triage.total_results}, "
        f"активных падений {len(data.triage.failed_tests)}."
    )
    print("Каталог — данные команды: не добавляйте его в git и не передавайте.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

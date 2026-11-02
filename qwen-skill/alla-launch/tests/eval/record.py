"""Запись прогона TestOps в кассету — для команды, локально.

    python tests/eval/record.py <launch_id> <out_dir>

Сбор — обычный ``pipeline.collect_launch`` скилла с настройками из ``<skill>/.env`` и
окружения ``ALLURE_*``; ответы инструмента ``testops_find_testresults`` MCP-сервера TestOps
раскладываются в кассету (``eval/cassette.py``): страницы прогона — в ``results.json``,
полные ответы (дерево шагов, фикстуры, вложения с содержимым) — в ``details/<id>.json``,
как есть. Заголовки запросов (токен) на диск не попадают. Каталог кассеты — данные команды:
держите его вне git и никому не передавайте.
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

if __package__ in (None, ""):  # запуск файлом: tests/ и scripts/ в sys.path
    _TESTS = Path(__file__).resolve().parents[1]
    for _path in (_TESTS, _TESTS.parent / "scripts"):
        if str(_path) not in sys.path:
            sys.path.insert(0, str(_path))

import httpx  # noqa: E402

_LAUNCH_AQL = re.compile(r"^launch = (\d+)$")
_ID_AQL = re.compile(r"^id = (\d+)$")
_EXPAND_KEYS = ("scenario", "fixtures", "attachments")


@dataclass
class Recording:
    """Ответы инструмента MCP TestOps, разложенные по частям кассеты."""

    launch_id: int | None = None
    pages: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    details: dict[int, dict[str, Any]] = field(default_factory=dict)

    def store(self, arguments: dict[str, Any], payload: Any) -> None:
        from alla_core.clients.mcp_client import tool_items

        aql = next((value.strip() for value in arguments.values()
                    if isinstance(value, str) and value.strip()), "")
        items = tool_items({"structuredContent": payload})
        if match := _LAUNCH_AQL.match(aql):
            self.launch_id = int(match[1])
            page = next((value for key, value in arguments.items()
                         if key in ("page", "pageNumber") and isinstance(value, int)), 0)
            self.pages[page] = items
        elif (match := _ID_AQL.match(aql)) and items:
            result_id = int(match[1])
            # Полный ответ (с expand) не затирается ответом без expand на тот же id.
            if not any(key in self.details.get(result_id, {}) for key in _EXPAND_KEYS):
                self.details[result_id] = items[0]

    def fixture(self) -> Any:
        from skill_fake_testops import LaunchFixture

        if self.launch_id is None:
            raise RuntimeError("Страницы прогона (aql launch = <id>) не записаны — прогон не получен.")
        results = [item for page in sorted(self.pages) for item in self.pages[page]]
        launch: dict[str, Any] = {"id": self.launch_id}
        if results and isinstance(results[0].get("launchName"), str):
            launch["name"] = results[0]["launchName"]
        return LaunchFixture(launch=launch, results=results, details=self.details)


@contextmanager
def recording_tool_calls(
    recording: Recording,
    inner: Callable[[bool], httpx.AsyncBaseTransport] | None = None,
) -> Generator[None]:
    """Складывать ответы ``testops_find_testresults`` в ``Recording``.

    Перехват — на уровне клиента (данные инструмента после разбора JSON-RPC), а не
    транспорта: так запись не зависит от того, ответил сервер JSON или потоком SSE.
    ``inner(verify)`` — свой транспорт httpx (тесты); по умолчанию — настоящий.
    """
    from alla_core.clients.mcp_client import TestOpsMcpClient

    real_call = TestOpsMcpClient._call_tool
    real_client = httpx.AsyncClient

    async def call(self: TestOpsMcpClient, arguments: dict[str, Any]) -> Any:
        payload = await real_call(self, arguments)
        recording.store(arguments, payload)
        return payload

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if inner is not None:
                kwargs["transport"] = inner(kwargs.get("verify", True))
            super().__init__(*args, **kwargs)

    TestOpsMcpClient._call_tool = call  # type: ignore[method-assign]
    httpx.AsyncClient = _Client  # type: ignore[misc]
    try:
        yield
    finally:
        TestOpsMcpClient._call_tool = real_call  # type: ignore[method-assign]
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
    recording = Recording()
    with recording_tool_calls(recording, inner):
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

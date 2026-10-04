#!/usr/bin/env python3
"""Фейковый TestOps как настоящий HTTP-сервер — для прогонов агента вне pytest.

Отвечает тем же ``FakeTestOps.handle``, что и тесты, поэтому данные стенда и тестов
одни. Слушает только 127.0.0.1. Каждый запрос пишется строкой JSON в журнал.

    python tests/fake_testops_server.py --fixture default --port 8777 --log requests.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_fake_testops import (  # noqa: E402
    FakeTestOps,
    LaunchFixture,
    default_launch,
    green_launch,
    info_only_launch,
    injection_launch,
    many_launch,
    scant_launch,
)

FIXTURES: dict[str, Callable[[], LaunchFixture]] = {
    "default": default_launch,
    "green": green_launch,
    "info_only": info_only_launch,
    "injection": injection_launch,
    "scant": scant_launch,
}


def build_fixture(spec: str) -> LaunchFixture:
    """``default`` | ``green`` | ``info_only`` | ``injection`` | ``scant`` | ``many:<число>``."""
    name, _, arg = spec.partition(":")
    if name == "many":
        return many_launch(int(arg or 40))
    if name not in FIXTURES or arg:
        raise ValueError(f"Неизвестный fixture: {spec}")
    return FIXTURES[name]()


class FakeTestOpsServer:
    """HTTP-обёртка над ``FakeTestOps`` в фоновом потоке."""

    def __init__(self, fixture: LaunchFixture, *, port: int = 0,
                 log_path: Path | None = None) -> None:
        self.fake = FakeTestOps(fixture)
        self.log_path = log_path
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler_class())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host!s}:{port}"

    def start(self) -> FakeTestOpsServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> FakeTestOpsServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _record(self, entry: dict[str, Any]) -> None:
        if self.log_path is None:
            return
        with self._lock, self.log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                request = httpx.Request(self.command, f"http://testops.local{self.path}",
                                        headers=dict(self.headers), content=body)
                try:
                    response = server.fake.handle(request)
                    content = response.read()
                    status = response.status_code
                    content_type = response.headers.get("content-type", "application/json")
                except Exception as error:  # noqa: BLE001 — ошибка fixture видна в журнале
                    content = json.dumps({"error": repr(error)}).encode("utf-8")
                    status, content_type = 500, "application/json"
                server._record({"time": time.time(), "method": self.command,
                                "path": self.path, "status": status})
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

        return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixture", default="default")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--log", type=Path)
    args = parser.parse_args(argv)
    server = FakeTestOpsServer(build_fixture(args.fixture), port=args.port, log_path=args.log)
    with server:
        print(f"ALLURE_ENDPOINT={server.endpoint}", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

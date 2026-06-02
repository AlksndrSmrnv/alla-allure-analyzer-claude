#!/usr/bin/env python3
"""ШАГ 3: сгенерировать HTML-отчёт по run_id.

HTML рендерит ``alla-server`` (тем же ``build_html_report_content``, что и
CLI/HTTP/MCP) и сохраняет его в ``alla.report`` — DSN живёт только на
сервере. Скрипт получает готовый HTML + метаданные через REST-эндпоинт
``POST /api/v1/skill/runs/{run_id}/report`` и при необходимости
дополнительно кладёт файл на диск пользователя (``--out`` /
``ALLURE_REPORTS_DIR``) — это путь на локальной машине, поэтому FS-запись
остаётся клиентской.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from alla.config import Settings

from _common import (
    EXIT_CONFIG,
    build_alla_client,
    error_envelope,
    exit_with_error,
    handle_api_error,
    load_settings,
    print_json,
)

logger = logging.getLogger("alla.skill.generate_report")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Сгенерировать HTML-отчёт по skill_run.",
    )
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument(
        "--out", default=None,
        help="Путь к выходному .html. Если не задан — определяется по ALLURE_REPORTS_DIR.",
    )
    return parser


def _interactive_disabled_reasons(settings: "Settings") -> list[str]:
    """Причины, по которым в HTML нет KB-кнопок и like/dislike.

    Дублирует серверную проверку для локальной диагностики: сервер
    возвращает свой список в ответе, но интерактив зависит ещё и от того,
    что прописано в `alla-skill/.env` пользователя.
    """
    reasons: list[str] = []
    if not getattr(settings, "kb_active", False):
        reasons.append("kb_inactive")
    elif not getattr(settings, "feedback_server_url", ""):
        reasons.append("feedback_server_url_empty")
    return reasons


def _write_to_disk(html: str, out: str | None, settings: "Settings", filename: str) -> str | None:
    """Сохранить HTML на локальный диск, если запрошено. Вернуть путь или None."""
    if out:
        out_path = pathlib.Path(out).expanduser().resolve()
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(html, encoding="utf-8")
            return str(out_path)
        except Exception as exc:
            logger.warning("Не удалось записать HTML в %s: %s", out_path, exc)
            return None
    if getattr(settings, "reports_dir", ""):
        try:
            reports_dir = pathlib.Path(settings.reports_dir).expanduser().resolve()
            reports_dir.mkdir(parents=True, exist_ok=True)
            disk_path = reports_dir / filename
            disk_path.write_text(html, encoding="utf-8")
            return str(disk_path)
        except Exception as exc:
            logger.warning("Не удалось сохранить HTML в %s: %s", settings.reports_dir, exc)
            return None
    return None


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings(require_kb_dsn=False, validate_testops=False)
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    from alla.clients.alla_api_client import AllaApiError

    try:
        with build_alla_client(settings) as client:
            response: dict[str, Any] = client.generate_skill_report(args.run_id)
    except AllaApiError as exc:
        handle_api_error(exc)

    html = response.get("html") or ""
    filename = response.get("report_filename") or f"alla_report_run_{args.run_id}.html"

    saved_to_disk = _write_to_disk(html, args.out, settings, filename)

    reasons = response.get("interactive_disabled_reasons")
    if reasons is None:
        reasons = _interactive_disabled_reasons(settings)
    if reasons:
        logger.warning(
            "HTML без интерактивных блоков: %s. Подними "
            "`python alla-skill/scripts/serve.py` и задай "
            "ALLURE_FEEDBACK_SERVER_URL, чтобы появились кнопки KB/like-dislike.",
            ", ".join(reasons),
        )

    print_json(
        {
            "ok": True,
            "run_id": args.run_id,
            "report_filename": filename,
            "report_url": response.get("report_url"),
            "saved_to_db": response.get("saved_to_db", False),
            "saved_to_disk": saved_to_disk,
            "html_size_bytes": response.get("html_size_bytes", len(html.encode("utf-8"))),
            "interactive_disabled_reasons": reasons,
        }
    )


if __name__ == "__main__":
    main(sys.argv[1:])

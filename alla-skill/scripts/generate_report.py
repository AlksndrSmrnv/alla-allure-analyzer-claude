#!/usr/bin/env python3
"""ШАГ 3: сгенерировать HTML-отчёт по run_id.

Тонкая обёртка:

* :func:`alla.services.skill_state_service.load_run`
* :func:`alla.services.agent_analysis_adapter.agent_to_llm_result` /
  :func:`alla.services.agent_analysis_adapter.agent_to_launch_summary` —
  упаковывают агентский анализ обратно в формат, который ожидает
  :func:`alla.report.html_report.generate_html_report`.
* :class:`alla.report.report_store.PostgresReportStore` — сохранение в БД.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import pathlib
import sys
from typing import Any

from _common import (
    EXIT_CONFIG,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    error_envelope,
    exit_with_error,
    get_pg_dsn,
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
    parser.add_argument(
        "--save-to-db", dest="save_to_db",
        action="store_true", default=True,
        help="Сохранить отчёт в alla.report (по умолчанию включено).",
    )
    parser.add_argument(
        "--no-save-to-db",
        dest="save_to_db",
        action="store_false",
    )
    return parser


def _build_filename(skill_run: Any) -> str:
    timestamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return f"alla_launch_{skill_run.launch_id}_run_{skill_run.run_id}_{timestamp}.html"


def _build_analysis_result(skill_run: Any) -> Any:
    from alla.orchestrator import AnalysisResult
    from alla.services.agent_analysis_adapter import (
        agent_to_launch_summary,
        agent_to_llm_result,
    )

    llm_result = None
    llm_summary = None
    if skill_run.agent_analysis is not None and skill_run.clustering_report is not None:
        llm_result = agent_to_llm_result(
            skill_run.agent_analysis,
            skill_run.clustering_report,
        )
        llm_summary = agent_to_launch_summary(skill_run.agent_analysis)

    return AnalysisResult(
        triage_report=skill_run.triage_report,
        clustering_report=skill_run.clustering_report,
        kb_results=skill_run.kb_results,
        kb_provenance=skill_run.kb_provenance,
        llm_result=llm_result,
        llm_launch_summary=llm_summary,
        feedback_contexts=skill_run.feedback_contexts,
        onboarding=skill_run.onboarding,
    )


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    from alla.report.html_report import generate_html_report
    from alla.report.report_store import PostgresReportStore
    from alla.services.skill_state_service import (
        SkillStateError,
        load_run,
        record_error,
        save_report,
    )

    dsn = get_pg_dsn(settings)
    try:
        skill_run = load_run(dsn=dsn, run_id=args.run_id)
    except SkillStateError as exc:
        exit_with_error(error_envelope(str(exc), run_id=args.run_id), EXIT_NOT_FOUND)
        return

    try:
        result = _build_analysis_result(skill_run)
        html = generate_html_report(
            result,
            endpoint=settings.endpoint,
            feedback_api_url="",
            server_url=settings.server_external_url,
        )
    except Exception as exc:
        try:
            record_error(
                dsn=dsn, run_id=args.run_id,
                error={"step": "generate_html", "message": str(exc)},
            )
        except Exception:
            pass
        exit_with_error(
            error_envelope(f"Не удалось сгенерировать HTML: {exc}", run_id=args.run_id),
            EXIT_ERROR,
        )
        return

    filename = _build_filename(skill_run)
    saved_to_disk: str | None = None
    if args.out:
        out_path = pathlib.Path(args.out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        saved_to_disk = str(out_path)
    elif settings.reports_dir:
        out_dir = pathlib.Path(settings.reports_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / filename
        out_path.write_text(html, encoding="utf-8")
        saved_to_disk = str(out_path)

    saved_to_db = False
    if args.save_to_db:
        try:
            store = PostgresReportStore(dsn=dsn)
            store.save(
                filename=filename,
                launch_id=skill_run.launch_id,
                html=html,
                project_id=skill_run.project_id,
            )
            saved_to_db = True
        except Exception as exc:
            logger.warning("Не удалось сохранить отчёт в БД: %s", exc)

    report_url = ""
    if settings.server_external_url:
        report_url = (
            f"{settings.server_external_url.rstrip('/')}/reports/{filename}"
        )
    elif settings.report_url:
        report_url = settings.report_url

    try:
        save_report(
            dsn=dsn,
            run_id=args.run_id,
            report_filename=filename,
            report_url=report_url or None,
        )
    except SkillStateError as exc:
        exit_with_error(
            error_envelope(
                f"Отчёт сгенерирован, но не удалось обновить skill_run: {exc}",
                run_id=args.run_id,
                report_filename=filename,
            ),
            EXIT_ERROR,
        )
        return

    print_json(
        {
            "ok": True,
            "run_id": args.run_id,
            "report_filename": filename,
            "report_url": report_url,
            "saved_to_db": saved_to_db,
            "saved_to_disk": saved_to_disk,
            "html_size_bytes": len(html.encode("utf-8")),
        }
    )


if __name__ == "__main__":
    main(sys.argv[1:])

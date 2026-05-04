#!/usr/bin/env python3
"""ШАГ 3: сгенерировать HTML-отчёт по run_id.

Тонкая обёртка над общими хелперами серверной alla, чтобы HTML-отчёт
скилл-режима был структурно идентичен тому, что строит
``alla.orchestrator.analyze_launch`` для CLI/HTTP/MCP:

* :func:`alla.services.skill_state_service.load_run` — поднять состояние run.
* :func:`alla.services.agent_analysis_adapter.agent_to_llm_result` /
  :func:`alla.services.agent_analysis_adapter.agent_to_launch_summary` —
  упаковывают агентский анализ в :class:`LLMAnalysisResult` /
  :class:`LLMLaunchSummary`.
* :func:`alla.orchestrator.build_onboarding_state` — пересчитывает
  onboarding на момент рендера (KB могла обновиться между ``fetch_clusters``
  и ``generate_report``).
* :func:`alla.app_support.build_html_report_content` — рендер HTML с тем
  же ``feedback_api_url`` и ``server_url``, что серверный путь.
* :func:`alla.app_support.resolve_report_url` — построение публичного URL.

Запись на диск/в БД выполняется здесь напрямую (а не через
``persist_generated_report``), чтобы независимо отслеживать успех FS-
и DB-шагов: если один упадёт, другой всё равно отработает, и в
итоговом JSON ``saved_to_disk`` / ``saved_to_db`` отражают реальный,
а не предполагаемый результат.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import pathlib
import sys
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from alla.config import Settings

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


def _build_analysis_result(skill_run: Any, settings: "Settings") -> Any:
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

    onboarding = _refresh_onboarding(settings, skill_run)

    return AnalysisResult(
        triage_report=skill_run.triage_report,
        clustering_report=skill_run.clustering_report,
        kb_results=skill_run.kb_results,
        kb_provenance=skill_run.kb_provenance,
        llm_result=llm_result,
        llm_launch_summary=llm_summary,
        feedback_contexts=skill_run.feedback_contexts,
        onboarding=onboarding,
    )


def _refresh_onboarding(settings: "Settings", skill_run: Any) -> Any:
    """Пересчитать ``OnboardingState`` на момент рендера отчёта.

    KB могла измениться между ``fetch_clusters`` и ``generate_report`` —
    серверный путь считает onboarding каждый раз заново внутри
    ``analyze_launch``. Чтобы skill-отчёт совпадал, делаем то же самое.
    Если KB недоступна / ``kb_active=False``, ``build_onboarding_state``
    вернёт ``KB_NOT_CONFIGURED`` без обращения к БД.
    """
    from alla.orchestrator import build_onboarding_state
    from alla.services.kb_lookup_service import lookup_kb_for_clusters

    kb_entries: list[Any] = []
    if settings.kb_active:
        try:
            fresh = lookup_kb_for_clusters(
                skill_run.triage_report,
                None,
                settings,
            )
            kb_entries = fresh.kb_entries
        except Exception as exc:
            logger.warning(
                "Не удалось обновить KB entries для onboarding: %s. "
                "Используются кэшированные данные skill_run.",
                exc,
            )
            return skill_run.onboarding

    return build_onboarding_state(
        settings,
        skill_run.project_id,
        skill_run.clustering_report,
        kb_entries=kb_entries,
    )


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    from alla.app_support import (
        build_html_report_content,
        resolve_report_url,
    )
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
        result = _build_analysis_result(skill_run, settings)
        html = build_html_report_content(result, settings=settings)
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
    saved_to_db = False
    sink_failures: list[str] = []
    fs_requested = bool(args.out) or bool(settings.reports_dir)
    db_requested = bool(args.save_to_db)

    # FS save: явный --out имеет приоритет над `ALLURE_REPORTS_DIR`.
    # `saved_to_disk` фиксируется только после успешного `write_text`,
    # чтобы при падении (например, нет прав на каталог) скрипт не
    # рапортовал о записи, которой не было.
    if args.out:
        out_path = pathlib.Path(args.out).expanduser().resolve()
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(html, encoding="utf-8")
            saved_to_disk = str(out_path)
        except Exception as exc:
            logger.warning("Не удалось записать HTML в %s: %s", out_path, exc)
            sink_failures.append(f"--out={out_path}: {exc}")
    elif settings.reports_dir:
        try:
            reports_dir = pathlib.Path(settings.reports_dir).expanduser().resolve()
            reports_dir.mkdir(parents=True, exist_ok=True)
            disk_path = reports_dir / filename
            disk_path.write_text(html, encoding="utf-8")
            saved_to_disk = str(disk_path)
        except Exception as exc:
            logger.warning(
                "Не удалось сохранить HTML в %s: %s",
                settings.reports_dir,
                exc,
            )
            sink_failures.append(f"reports_dir={settings.reports_dir}: {exc}")

    # DB save — независимо от FS, чтобы падение одного не маскировало
    # успех другого.
    if db_requested:
        try:
            from alla.app_support import calculate_llm_token_usage

            report_store = PostgresReportStore(dsn=dsn)
            report_store.save(
                filename,
                skill_run.launch_id,
                html,
                skill_run.project_id,
                token_usage=calculate_llm_token_usage(result),
            )
            saved_to_db = True
        except Exception as exc:
            logger.warning("Не удалось сохранить отчёт в БД: %s", exc)
            sink_failures.append(f"postgres: {exc}")

    # Если у пользователя был хотя бы один запрошенный sink (FS или DB)
    # и все запрошенные упали — это hard error: HTML нигде не лежит,
    # ссылка из `report_url` указывала бы в никуда. Записываем ошибку
    # в skill_run и выходим non-zero ДО того, как `save_report` пометит
    # run как `reported`.
    if (fs_requested or db_requested) and not (saved_to_disk or saved_to_db):
        joined = "; ".join(sink_failures) or "нет успешных sinks"
        try:
            record_error(
                dsn=dsn, run_id=args.run_id,
                error={"step": "persist_report", "message": joined},
            )
        except Exception:
            pass
        exit_with_error(
            error_envelope(
                f"Не удалось сохранить отчёт ни в один sink: {joined}",
                run_id=args.run_id,
                report_filename=filename,
            ),
            EXIT_ERROR,
        )
        return

    report_url = resolve_report_url(settings, report_filename=filename)

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

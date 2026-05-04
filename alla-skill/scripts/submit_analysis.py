#!/usr/bin/env python3
"""Принять агентский анализ через stdin и записать в ``alla.skill_run``.

Тонкая обёртка над :mod:`alla.services.agent_analysis_adapter`
(валидация) и :mod:`alla.services.skill_state_service` (запись).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from _common import (
    EXIT_CONFIG,
    EXIT_NOT_FOUND,
    EXIT_VALIDATION,
    error_envelope,
    exit_with_error,
    get_pg_dsn,
    load_settings,
    parse_stdin_json,
    print_json,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Записать агентский анализ кластеров в alla.skill_run.",
    )
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument(
        "--input",
        default="-",
        help="Путь к JSON-файлу или '-' для stdin (default '-').",
    )
    return parser


def _read_payload(source: str) -> Any:
    if source == "-":
        return parse_stdin_json()
    with open(source, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    try:
        payload = _read_payload(args.input)
    except Exception as exc:
        exit_with_error(
            error_envelope(f"Не удалось прочитать payload: {exc}"),
            EXIT_VALIDATION,
        )
        return

    from alla.services.agent_analysis_adapter import (
        AgentAnalysisError,
        validate_agent_payload,
    )
    from alla.services.skill_state_service import (
        SkillStateError,
        load_run,
        save_agent_analysis,
    )

    dsn = get_pg_dsn(settings)
    try:
        skill_run = load_run(dsn=dsn, run_id=args.run_id)
    except SkillStateError as exc:
        exit_with_error(
            error_envelope(str(exc), run_id=args.run_id),
            EXIT_NOT_FOUND,
        )
        return

    expected_ids: list[str] = []
    if skill_run.clustering_report is not None:
        expected_ids = [c.cluster_id for c in skill_run.clustering_report.clusters]

    try:
        missing, extra = validate_agent_payload(
            payload,
            expected_cluster_ids=expected_ids,
        )
    except AgentAnalysisError as exc:
        exit_with_error(
            error_envelope(
                f"Невалидный agent payload: {exc}",
                run_id=args.run_id,
            ),
            EXIT_VALIDATION,
        )
        return

    summary_text = (
        payload.get("launch_summary", {}).get("summary_text")
        if isinstance(payload, dict)
        else ""
    ) or ""

    try:
        save_agent_analysis(
            dsn=dsn,
            run_id=args.run_id,
            agent_analysis=payload,
            agent_summary_text=summary_text,
        )
    except SkillStateError as exc:
        exit_with_error(
            error_envelope(
                f"Не удалось записать анализ: {exc}",
                run_id=args.run_id,
            ),
            EXIT_NOT_FOUND,
        )
        return

    print_json(
        {
            "ok": True,
            "run_id": args.run_id,
            "clusters_received": len(payload.get("clusters", {})),
            "clusters_expected": len(expected_ids),
            "missing_cluster_ids": missing,
            "extra_cluster_ids": extra,
        }
    )


if __name__ == "__main__":
    main(sys.argv[1:])

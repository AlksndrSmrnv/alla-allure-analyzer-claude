"""Сервис управления состоянием skill-режима в таблице ``alla.skill_run``.

Все скрипты в ``alla-skill/scripts/`` обмениваются данными через
``alla.skill_run``: ``fetch_clusters`` создаёт row, ``submit_analysis``
дописывает агентский анализ, ``generate_report`` фиксирует сгенерированный
HTML, ``push_to_testops`` фиксирует push.

Сервис централизует JSONB-сериализацию pydantic-моделей и состояние
status-машины ``pending → clustered → analyzed → reported → pushed``
(плюс ``failed`` при ошибке).

DDL таблицы — ``alla-skill/sql/skill_run_schema.sql``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from alla.knowledge.feedback_models import FeedbackClusterContext
from alla.knowledge.models import KBMatchResult
from alla.models.clustering import ClusteringReport
from alla.models.onboarding import OnboardingState
from alla.models.testops import TriageReport
from alla.services.kb_lookup_service import KBStageResult

logger = logging.getLogger(__name__)

__all__ = [
    "SKILL_RUN_SCHEMA_VERSION",
    "SkillRunStatus",
    "SkillRun",
    "SkillStateError",
    "create_run",
    "load_run",
    "update_status",
    "record_error",
    "save_agent_analysis",
    "save_report",
    "save_push_result",
]


SKILL_RUN_SCHEMA_VERSION = 1


class SkillRunStatus:
    """Допустимые значения колонки ``alla.skill_run.status``."""

    PENDING = "pending"
    CLUSTERED = "clustered"
    ANALYZED = "analyzed"
    REPORTED = "reported"
    PUSHED = "pushed"
    FAILED = "failed"


class SkillStateError(RuntimeError):
    """Ошибка чтения/записи состояния skill_run."""


@dataclass
class SkillRun:
    """In-memory представление строки ``alla.skill_run``."""

    run_id: int
    schema_version: int
    status: str
    launch_id: int
    project_id: int | None
    launch_name: str | None
    triage_report: TriageReport
    clustering_report: ClusteringReport | None
    kb_results: dict[str, list[KBMatchResult]] = field(default_factory=dict)
    kb_provenance: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    feedback_contexts: dict[str, FeedbackClusterContext] = field(default_factory=dict)
    onboarding: OnboardingState = field(default_factory=OnboardingState)
    agent_analysis: dict[str, Any] | None = None
    agent_summary_text: str | None = None
    agent_submitted_at: datetime | None = None
    report_filename: str | None = None
    report_url: str | None = None
    report_generated_at: datetime | None = None
    push_result: dict[str, Any] | None = None
    pushed_at: datetime | None = None
    error: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_run(
    *,
    dsn: str,
    triage_report: TriageReport,
    clustering_report: ClusteringReport | None,
    kb_stage: KBStageResult,
    onboarding: OnboardingState,
) -> int:
    """INSERT новую строку ``alla.skill_run`` и вернуть ``run_id``.

    Состояние выставляется в ``clustered``, потому что по выходу из
    ``fetch_clusters`` мы уже имеем кластеры и KB-совпадения; агентский
    анализ ещё не пришёл.
    """
    triage_json = triage_report.model_dump(mode="json")
    clustering_json = (
        clustering_report.model_dump(mode="json")
        if clustering_report is not None
        else None
    )
    kb_results_json = _serialize_kb_results(kb_stage.kb_results)
    kb_provenance_json = _serialize_kb_provenance(kb_stage.kb_provenance)
    feedback_ctx_json = _serialize_feedback_contexts(
        kb_stage.feedback_contexts,
    )
    onboarding_json = onboarding.model_dump(mode="json")

    query = """
        INSERT INTO alla.skill_run (
            schema_version, status, launch_id, project_id, launch_name,
            triage_json, clustering_json,
            kb_results_json, kb_provenance_json, feedback_ctx_json,
            onboarding_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING run_id
    """
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    (
                        SKILL_RUN_SCHEMA_VERSION,
                        SkillRunStatus.CLUSTERED,
                        triage_report.launch_id,
                        triage_report.project_id,
                        triage_report.launch_name,
                        Jsonb(triage_json),
                        Jsonb(clustering_json) if clustering_json else None,
                        Jsonb(kb_results_json),
                        Jsonb(kb_provenance_json),
                        Jsonb(feedback_ctx_json),
                        Jsonb(onboarding_json),
                    ),
                )
                row = cur.fetchone()
                conn.commit()
    except Exception as exc:  # pragma: no cover - DB errors
        raise SkillStateError(
            f"Не удалось создать alla.skill_run: {exc}"
        ) from exc

    if row is None:
        raise SkillStateError("INSERT не вернул run_id")
    run_id = int(row[0])
    logger.info(
        "skill_run #%d создан (launch_id=%d, project_id=%s)",
        run_id,
        triage_report.launch_id,
        triage_report.project_id,
    )
    return run_id


def load_run(*, dsn: str, run_id: int) -> SkillRun:
    """SELECT строку по ``run_id`` и десериализовать в :class:`SkillRun`."""
    query = """
        SELECT run_id, schema_version, status,
               launch_id, project_id, launch_name,
               triage_json, clustering_json,
               kb_results_json, kb_provenance_json, feedback_ctx_json,
               onboarding_json,
               agent_analysis_json, agent_summary_text, agent_submitted_at,
               report_filename, report_url, report_generated_at,
               push_result_json, pushed_at,
               error_json,
               created_at, updated_at
        FROM alla.skill_run
        WHERE run_id = %s
    """
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query, (run_id,))
                row = cur.fetchone()
    except Exception as exc:  # pragma: no cover - DB errors
        raise SkillStateError(
            f"Не удалось прочитать alla.skill_run: {exc}"
        ) from exc

    if row is None:
        raise SkillStateError(f"alla.skill_run run_id={run_id} не найден")

    schema_version = int(row[1])
    if schema_version != SKILL_RUN_SCHEMA_VERSION:
        raise SkillStateError(
            f"Несовместимый schema_version={schema_version} "
            f"в run_id={run_id} (ожидалось {SKILL_RUN_SCHEMA_VERSION})"
        )

    triage_report = TriageReport.model_validate(row[6])
    clustering_report = (
        ClusteringReport.model_validate(row[7]) if row[7] else None
    )
    onboarding = (
        OnboardingState.model_validate(row[11]) if row[11] else OnboardingState()
    )
    kb_results = _deserialize_kb_results(row[8] or {})
    kb_provenance = _deserialize_kb_provenance(row[9] or {})
    feedback_contexts = _deserialize_feedback_contexts(row[10] or {})

    return SkillRun(
        run_id=int(row[0]),
        schema_version=schema_version,
        status=row[2],
        launch_id=int(row[3]),
        project_id=row[4],
        launch_name=row[5],
        triage_report=triage_report,
        clustering_report=clustering_report,
        kb_results=kb_results,
        kb_provenance=kb_provenance,
        feedback_contexts=feedback_contexts,
        onboarding=onboarding,
        agent_analysis=row[12],
        agent_summary_text=row[13],
        agent_submitted_at=row[14],
        report_filename=row[15],
        report_url=row[16],
        report_generated_at=row[17],
        push_result=row[18],
        pushed_at=row[19],
        error=row[20],
        created_at=row[21],
        updated_at=row[22],
    )


def save_agent_analysis(
    *,
    dsn: str,
    run_id: int,
    agent_analysis: dict[str, Any],
    agent_summary_text: str,
) -> None:
    """Записать агентский анализ и перевести status в ``analyzed``."""
    query = """
        UPDATE alla.skill_run
        SET agent_analysis_json = %s,
            agent_summary_text = %s,
            agent_submitted_at = now(),
            status = %s,
            error_json = NULL
        WHERE run_id = %s
    """
    _execute_update(
        dsn,
        query,
        (
            Jsonb(agent_analysis),
            agent_summary_text,
            SkillRunStatus.ANALYZED,
            run_id,
        ),
        run_id=run_id,
    )


def save_report(
    *,
    dsn: str,
    run_id: int,
    report_filename: str,
    report_url: str | None,
) -> None:
    """Записать данные сгенерированного отчёта и перевести status в ``reported``."""
    query = """
        UPDATE alla.skill_run
        SET report_filename = %s,
            report_url = %s,
            report_generated_at = now(),
            status = %s,
            error_json = NULL
        WHERE run_id = %s
    """
    _execute_update(
        dsn,
        query,
        (
            report_filename,
            report_url,
            SkillRunStatus.REPORTED,
            run_id,
        ),
        run_id=run_id,
    )


def save_push_result(
    *,
    dsn: str,
    run_id: int,
    push_result: dict[str, Any],
) -> None:
    """Записать результат push'а и перевести status в ``pushed``."""
    query = """
        UPDATE alla.skill_run
        SET push_result_json = %s,
            pushed_at = now(),
            status = %s,
            error_json = NULL
        WHERE run_id = %s
    """
    _execute_update(
        dsn,
        query,
        (
            Jsonb(push_result),
            SkillRunStatus.PUSHED,
            run_id,
        ),
        run_id=run_id,
    )


def update_status(*, dsn: str, run_id: int, status: str) -> None:
    """Установить произвольный status (используется в служебных скриптах)."""
    _execute_update(
        dsn,
        "UPDATE alla.skill_run SET status = %s WHERE run_id = %s",
        (status, run_id),
        run_id=run_id,
    )


def record_error(
    *,
    dsn: str,
    run_id: int,
    error: dict[str, Any],
) -> None:
    """Записать ``error_json`` и перевести status в ``failed``.

    Скрипты вызывают это в обработчике исключений верхнего уровня.
    """
    _execute_update(
        dsn,
        "UPDATE alla.skill_run "
        "SET error_json = %s, status = %s WHERE run_id = %s",
        (Jsonb(error), SkillRunStatus.FAILED, run_id),
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _execute_update(
    dsn: str,
    query: str,
    params: tuple[Any, ...],
    *,
    run_id: int,
) -> None:
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                if cur.rowcount == 0:
                    raise SkillStateError(
                        f"alla.skill_run run_id={run_id} не найден"
                    )
                conn.commit()
    except SkillStateError:
        raise
    except Exception as exc:  # pragma: no cover - DB errors
        raise SkillStateError(
            f"Не удалось обновить alla.skill_run #{run_id}: {exc}"
        ) from exc


def _serialize_kb_results(
    kb_results: dict[str, list[KBMatchResult]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        cluster_id: [match.model_dump(mode="json") for match in matches]
        for cluster_id, matches in kb_results.items()
    }


def _deserialize_kb_results(
    payload: dict[str, list[dict[str, Any]]],
) -> dict[str, list[KBMatchResult]]:
    return {
        cluster_id: [KBMatchResult.model_validate(item) for item in matches]
        for cluster_id, matches in (payload or {}).items()
    }


def _serialize_kb_provenance(
    kb_provenance: dict[str, tuple[int, int, int]],
) -> dict[str, list[int]]:
    return {
        cluster_id: list(values) for cluster_id, values in kb_provenance.items()
    }


def _deserialize_kb_provenance(
    payload: dict[str, list[int]],
) -> dict[str, tuple[int, int, int]]:
    result: dict[str, tuple[int, int, int]] = {}
    for cluster_id, values in (payload or {}).items():
        if isinstance(values, list) and len(values) == 3:
            result[cluster_id] = (
                int(values[0]),
                int(values[1]),
                int(values[2]),
            )
    return result


def _serialize_feedback_contexts(
    feedback_contexts: dict[str, FeedbackClusterContext],
) -> dict[str, dict[str, Any]]:
    return {
        cluster_id: ctx.model_dump(mode="json")
        for cluster_id, ctx in feedback_contexts.items()
    }


def _deserialize_feedback_contexts(
    payload: dict[str, dict[str, Any]],
) -> dict[str, FeedbackClusterContext]:
    return {
        cluster_id: FeedbackClusterContext.model_validate(value)
        for cluster_id, value in (payload or {}).items()
    }

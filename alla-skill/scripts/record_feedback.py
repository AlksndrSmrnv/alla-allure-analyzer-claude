#!/usr/bin/env python3
"""Like/dislike feedback на kb_match для exact-feedback memory.

Использует issue signature, сохранённый ``fetch_clusters`` для
кластера (``feedback_ctx_json``). Если signature нет — запись отклоняется.
"""

from __future__ import annotations

import argparse
import sys

from _common import (
    EXIT_CONFIG,
    EXIT_NOT_FOUND,
    EXIT_VALIDATION,
    build_alla_client,
    error_envelope,
    exit_with_error,
    get_pg_dsn,
    handle_api_error,
    load_settings,
    print_json,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Like/dislike feedback по kb_match.",
    )
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument("--kb-entry-id", required=True, type=int)
    parser.add_argument("--vote", required=True, choices=("like", "dislike"))
    parser.add_argument("--note", default=None)
    parser.add_argument(
        "--scope",
        choices=("base", "step"),
        default="base",
        help="Какую сигнатуру использовать: base (issue) или step (step-aware).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        exit_with_error(error_envelope(f"Ошибка конфигурации: {exc}"), EXIT_CONFIG)
        return

    from alla.clients.alla_api_client import AllaApiError
    from alla.knowledge.feedback_models import FeedbackRequest, FeedbackVote
    from alla.services.skill_state_service import SkillStateError, load_run

    dsn = get_pg_dsn(settings)
    try:
        skill_run = load_run(dsn=dsn, run_id=args.run_id)
    except SkillStateError as exc:
        exit_with_error(error_envelope(str(exc), run_id=args.run_id), EXIT_NOT_FOUND)
        return

    feedback_ctx = skill_run.feedback_contexts.get(args.cluster_id)
    if feedback_ctx is None:
        exit_with_error(
            error_envelope(
                f"Для кластера {args.cluster_id!r} нет issue signature — "
                "feedback не может быть записан.",
                run_id=args.run_id,
            ),
            EXIT_VALIDATION,
        )
        return

    if args.scope == "step":
        if feedback_ctx.step_issue_signature is None:
            exit_with_error(
                error_envelope(
                    "Step-aware signature недоступна для этого кластера. "
                    "Используй --scope base.",
                    cluster_id=args.cluster_id,
                ),
                EXIT_VALIDATION,
            )
            return
        signature = feedback_ctx.step_issue_signature
    else:
        signature = feedback_ctx.base_issue_signature

    audit_text = feedback_ctx.audit_text
    if args.note:
        audit_text = f"{audit_text}\n\n[note]\n{args.note}"

    request = FeedbackRequest(
        kb_entry_id=args.kb_entry_id,
        audit_text=audit_text[:2000],
        vote=FeedbackVote(args.vote),
        issue_signature_hash=signature.signature_hash,
        issue_signature_version=signature.version,
        launch_id=skill_run.launch_id,
        cluster_id=args.cluster_id,
    )

    try:
        with build_alla_client(settings) as client:
            response = client.submit_feedback(request)
    except AllaApiError as exc:
        handle_api_error(exc)

    print_json(
        {
            "ok": True,
            "kb_entry_id": response.kb_entry_id,
            "vote": response.vote.value,
            "feedback_id": response.feedback_id,
            "created": response.created,
            "scope": args.scope,
        }
    )


if __name__ == "__main__":
    main(sys.argv[1:])

"""prepare saves the enriched signature; feedback reuses it from the compact run."""

import json
from datetime import date, datetime
from pathlib import Path

from skill_fixtures import without_libmagic  # noqa: F401

from alla_core.config import Settings
from alla_core.models.clustering import ClusteringReport, ClusterSignature, FailureCluster
from alla_core.models.testops import FailedTestSummary, TriageReport
from alla_skill_lib import cli, feedback, workspace as ws
from alla_skill_lib.kb import cluster_signature
from alla_skill_lib.pipeline import LaunchData
from alla_skill_lib.report import load_models


def test_prepare_and_feedback_keep_saved_signature_without_transient_fields(tmp_path: Path) -> None:
    summary = FailedTestSummary(
        test_result_id=1, name="test", status="failed", status_message="request failed",
        log_snippet="--- [файл: app.log] ---\nbackend pool exhausted\n\n"
                    "[... обрезано: было 9000 символов, оставлено 1000 ...]",
        log_selection_error="request failed", log_selection_truncated=True,
    )
    cluster = FailureCluster(
        cluster_id="pool", label="Pool exhausted", signature=ClusterSignature(),
        member_test_ids=[1], member_count=1, representative_test_id=1,
        example_message="request failed",
    )
    expected = cluster_signature(cluster, {1: summary})
    triage = TriageReport(launch_id=1, total_results=1, failed_count=1, failed_tests=[summary])
    clustering = ClusteringReport(launch_id=1, total_failures=1, cluster_count=1, clusters=[cluster])
    paths = ws.create_run_dir(tmp_path / "alla-reports", 1, datetime(2026, 10, 3))
    cli._write_run(paths, LaunchData(triage, clustering), Settings(), tmp_path)
    run = ws.read_json(paths.run_json)
    entry = run["clusters"][0]
    assert entry["signature"] == expected
    assert "log_selection_" not in paths.run_json.read_text(encoding="utf-8")
    restored, _ = load_models(run)
    assert restored.failed_tests[0].log_snippet is None
    assert restored.failed_tests[0].status_trace is None

    ws.write_text(paths.feedback("01"), (
        "НАЗВАНИЕ: Исчерпан пул\nПРИЧИНА: приложение — backend pool exhausted\n"
        "КАК ИСПРАВИТЬ:\n1. Восстановить соединения в пуле.\n"
        "ПРИЗНАК: backend pool exhausted\n"
    ))
    status, body = feedback.remember(paths, run, entry, None, date(2026, 10, 3))
    assert status == "saved", body
    kb_file = next((tmp_path / "alla-kb").glob("*.json"))
    record = json.loads(kb_file.read_text(encoding="utf-8"))
    assert record["confirmed_signatures"] == [expected]
    status, body = feedback.reject(paths, run, entry, record["id"])
    assert status == "saved", body
    record = json.loads(kb_file.read_text(encoding="utf-8"))
    assert record["rejected_signatures"] == [expected]

"""Тесты CLI-вывода кластеров."""

from alla.cli_output import print_clustering_report

from conftest import (
    make_clustering_report,
    make_failed_test_summary,
    make_failure_cluster,
)


def test_print_clustering_report_shows_http_section_separately(capsys) -> None:
    cluster = make_failure_cluster(
        cluster_id="c-http",
        representative_test_id=1,
        member_test_ids=[1],
        member_count=1,
    )
    report = make_clustering_report(
        clusters=[cluster],
        cluster_count=1,
        total_failures=1,
    )
    failed_tests = [
        make_failed_test_summary(
            test_result_id=1,
            log_snippet=(
                "--- [файл: app.log] ---\n"
                "retry budget exhausted while saving order\n"
                "\n"
                "--- [HTTP: response.json] ---\n"
                "HTTP статус: 503\n"
                "error: Service unavailable"
            ),
        )
    ]

    print_clustering_report(report, failed_tests=failed_tests)
    output = capsys.readouterr().out

    assert "--- app.log ---" in output
    assert "--- HTTP: response.json ---" in output
    assert "--- [HTTP: response.json] ---" not in output

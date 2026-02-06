"""Тесты рамочного текстового вывода кластеров в CLI."""

from __future__ import annotations

from alla.cli import _print_clustering_report
from alla.models.clustering import ClusterSignature, ClusteringReport, FailureCluster


def _build_cluster(
    *,
    cluster_id: str,
    label: str,
    member_test_ids: list[int],
    example_message: str | None = None,
) -> FailureCluster:
    return FailureCluster(
        cluster_id=cluster_id,
        label=label,
        signature=ClusterSignature(),
        member_test_ids=member_test_ids,
        member_count=len(member_test_ids),
        example_message=example_message,
    )


def test_single_cluster_box_contains_all_core_lines(capsys) -> None:
    report = ClusteringReport(
        launch_id=1,
        total_failures=3,
        cluster_count=1,
        clusters=[
            _build_cluster(
                cluster_id="abc1234567890def",
                label="NullPointerException",
                member_test_ids=[101, 102, 103],
                example_message="first line\nsecond\tline",
            )
        ],
    )

    _print_clustering_report(report)
    output = capsys.readouterr().out

    assert "=== Failure Clusters (1 unique problems from 3 failures) ===" in output
    assert output.count("╔") == 1
    assert output.count("╚") == 1
    assert "Cluster #1: NullPointerException (3 tests)" in output
    assert "Cluster ID: abc1234567890def" in output
    assert "Example: first line second line" in output
    assert "Tests: 101, 102, 103" in output


def test_multiple_clusters_have_separate_boxes_with_blank_line_between(capsys) -> None:
    report = ClusteringReport(
        launch_id=1,
        total_failures=2,
        cluster_count=2,
        clusters=[
            _build_cluster(cluster_id="id-1", label="First", member_test_ids=[1]),
            _build_cluster(cluster_id="id-2", label="Second", member_test_ids=[2]),
        ],
    )

    _print_clustering_report(report)
    output = capsys.readouterr().out

    assert output.count("╔") == 2
    assert output.count("╚") == 2
    assert "╝\n\n╔" in output


def test_cluster_without_example_does_not_render_example_line(capsys) -> None:
    report = ClusteringReport(
        launch_id=1,
        total_failures=1,
        cluster_count=1,
        clusters=[
            _build_cluster(
                cluster_id="no-example",
                label="Single failure",
                member_test_ids=[42],
            )
        ],
    )

    _print_clustering_report(report)
    output = capsys.readouterr().out

    assert output.count("╔") == 1
    assert output.count("╚") == 1
    assert "Example:" not in output


def test_example_message_is_truncated_to_200_chars(capsys) -> None:
    long_message = "x" * 250
    report = ClusteringReport(
        launch_id=1,
        total_failures=1,
        cluster_count=1,
        clusters=[
            _build_cluster(
                cluster_id="truncate-example",
                label="Long message",
                member_test_ids=[7],
                example_message=long_message,
            )
        ],
    )

    _print_clustering_report(report)
    output = capsys.readouterr().out
    expected = f"Example: {'x' * 200}..."

    assert expected in output
    assert f"Example: {'x' * 201}" not in output


def test_member_test_ids_are_limited_to_first_10_with_ellipsis(capsys) -> None:
    report = ClusteringReport(
        launch_id=1,
        total_failures=12,
        cluster_count=1,
        clusters=[
            _build_cluster(
                cluster_id="many-tests",
                label="Massive cluster",
                member_test_ids=list(range(1, 13)),
            )
        ],
    )

    _print_clustering_report(report)
    output = capsys.readouterr().out

    assert "Tests: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, ..." in output


def test_cyrillic_label_is_rendered_inside_box(capsys) -> None:
    label = "Ошибка авторизации в сервисе"
    report = ClusteringReport(
        launch_id=1,
        total_failures=1,
        cluster_count=1,
        clusters=[
            _build_cluster(
                cluster_id="ru-label",
                label=label,
                member_test_ids=[1001],
            )
        ],
    )

    _print_clustering_report(report)
    output = capsys.readouterr().out

    assert label in output
    assert output.count("╔") == 1
    assert output.count("╚") == 1

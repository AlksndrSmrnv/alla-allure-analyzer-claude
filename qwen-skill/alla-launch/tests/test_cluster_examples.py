"""Примеры кластера: типичный, наиболее отличающийся и самый информативный."""

from __future__ import annotations

import re

import pytest

import skill_fixtures  # noqa: F401  # scripts/ в sys.path

from alla_core.models.clustering import ClusteringReport
from alla_core.models.testops import FailedTestSummary
from alla_core.services.clustering_service import ClusteringService, select_examples
from alla_skill_lib.report import load_models

ASSERT = "expected: <200> but was: <500>"


def _failure(test_id: int, log: str | None = None, message: str = ASSERT,
             step: str = "Отправить запрос POST /orders") -> FailedTestSummary:
    return FailedTestSummary(test_result_id=test_id, name=f"t{test_id}", status="failed",
                             status_message=message, failed_step_path=step, log_snippet=log)


def _log(*errors: str) -> str:
    return "--- [файл: app.log] ---\n" + "\n\n".join(
        f"[строка {n}]\n2026-10-03 10:00:0{n} [ERROR] {error}" for n, error in enumerate(errors, 1))


def _roles(failures: list[FailedTestSummary]) -> list[tuple[str, int]]:
    report = ClusteringService().cluster_failures(1, failures)
    cluster, = [c for c in report.clusters if c.member_count == len(failures)]
    return [(example.role, example.test_result_id) for example in cluster.examples]


def test_same_assertion_with_different_server_errors_gets_both_shown() -> None:
    import asyncio

    from alla_core.config import Settings
    from alla_skill_lib.pipeline import collect_launch
    from eval.cassette import replay
    from eval.corpus_dev import same_assertion_db_vs_npe

    case = same_assertion_db_vs_npe()
    group = {test: g["id"] for g in case.labels["groups"] for test in g["tests"]}
    settings = Settings.load(environ={"ALLURE_ENDPOINT": "https://testops.example",
                                      "ALLURE_TOKEN": "token"})
    with replay(case.fixture):
        data = asyncio.run(collect_launch(case.fixture.launch["id"], settings))
    assert data.clustering is not None
    cluster, = data.clustering.clusters  # нынешний алгоритм склеивает обе группы

    assert [example.role for example in cluster.examples] == ["typical", "different"]
    assert {group[example.test_result_id] for example in cluster.examples} == {
        "orders-500-db", "orders-500-npe"}


def test_identical_failures_give_one_example() -> None:
    log = _log("OrderService: failed to create order")
    assert _roles([_failure(1, log), _failure(2, log), _failure(3, log)]) == [("typical", 1)]


def test_singleton_and_empty_clusters_have_their_test_as_the_example() -> None:
    report = ClusteringService().cluster_failures(
        1, [_failure(1), FailedTestSummary(test_result_id=9, name="silent", status="failed")])
    by_id = {c.representative_test_id: c for c in report.clusters}
    assert [(e.role, e.test_result_id) for e in by_id[9].examples] == [("typical", 9)]


def test_medoid_ties_and_informative_example() -> None:
    def distance(a: int, b: int) -> float:
        return 0.0 if a == b else 0.2

    failures = [_failure(10 + i, log) for i, log in enumerate(
        [None, None, _log("first"), _log("second", "third", "fourth")])]
    documents = ([ASSERT] * 4, ["step"] * 4, ["", "", "first", "second third fourth"])
    examples = select_examples([0, 1, 2, 3], failures, distance, documents)

    # Ничья по сумме расстояний — меньший id. Самый далёкий (ничья — 11) ничем не
    # отличается от типичного — не берётся; информативный — больше всего событий-ошибок.
    assert [(e.role, e.test_result_id) for e in examples] == [
        ("typical", 10), ("informative", 13)]


def test_farthest_example_is_taken_only_when_it_really_differs() -> None:
    far = {(0, 2): 0.9, (1, 2): 0.8, (0, 1): 0.1}

    def distance(a: int, b: int) -> float:
        return 0.0 if a == b else far[tuple(sorted((a, b)))]  # type: ignore[index]

    failures = [_failure(20 + i) for i in range(3)]
    differing = ([ASSERT, ASSERT, "expected: <200> but was: <502>"], ["s"] * 3, [""] * 3)
    examples = select_examples([0, 1, 2], failures, distance, differing)
    assert [(e.role, e.test_result_id) for e in examples] == [("typical", 21), ("different", 22)]

    same = ([ASSERT] * 3, ["s"] * 3, [""] * 3)  # отличие только в расстоянии — один пример
    assert len(select_examples([0, 1, 2], failures, distance, same)) == 1


def test_old_run_json_without_examples_still_loads() -> None:
    report = ClusteringReport.model_validate({
        "launch_id": 1, "total_failures": 1, "cluster_count": 1,
        "clusters": [{"cluster_id": "c", "label": "x", "signature": {},
                      "member_test_ids": [1], "member_count": 1, "representative_test_id": 1}]})
    assert report.clusters[0].examples == []
    triage = {"launch_id": 1, "total_results": 1, "failed_tests": []}
    _triage, clustering = load_models({"triage": triage, "clustering": report.model_dump()})
    assert clustering is not None and clustering.clusters[0].examples == []


def test_logs_differing_only_in_numbers_are_not_different_problems() -> None:
    def distance(a: int, b: int) -> float:
        return 0.0 if a == b else 0.3

    failures = [_failure(30 + i) for i in range(2)]
    logs = ["ERROR [nio-8080-exec-1] Worker.run(Worker.java:40) state stuck",
            "ERROR [nio-8080-exec-7] Worker.run(Worker.java:41) state stuck"]
    assert len(select_examples([0, 1], failures, distance, ([ASSERT] * 2, ["s"] * 2, logs))) == 1
    other = [logs[0], "ERROR [nio-8080-exec-7] Pool.get(Pool.java:12) pool exhausted"]
    assert len(select_examples([0, 1], failures, distance, ([ASSERT] * 2, ["s"] * 2, other))) == 2


@pytest.mark.parametrize(("first", "second"), [
    ("gateway failed error_code=10001", "gateway failed error_code=10002"),
    ("ORA-01017: invalid username/password", "ORA-12541: invalid username/password"),
    ("upstream answered HTTP 401", "upstream answered HTTP 403"),
    ("POST /orders -> 401", "POST /orders -> 403"),
])
def test_logs_with_different_error_codes_stay_different(first: str, second: str) -> None:
    def distance(a: int, b: int) -> float:
        return 0.0 if a == b else 0.3

    failures = [_failure(40, _log(first)), _failure(41, _log(second))]
    # Документ лога кластеризации уже без длинных чисел и, для сравнения, без цифр.
    docs = [re.sub(r"\d+", "#", first), re.sub(r"\d+", "#", second)]
    examples = select_examples([0, 1], failures, distance, ([ASSERT] * 2, ["s"] * 2, docs))
    assert [e.test_result_id for e in examples] == [40, 41]


def test_duplicate_of_a_chosen_log_does_not_hide_a_third_error() -> None:
    far = {frozenset({0, 1}): 0.3, frozenset({0, 2}): 0.2, frozenset({0, 3}): 0.2}

    def distance(a: int, b: int) -> float:
        return 0.0 if a == b else far.get(frozenset({a, b}), 0.5)

    # Логи A, B, A, C: у повтора A больше всего ошибок, но он уже показан типичным.
    logs = [_log("pool exhausted"), _log("discount is null"),
            _log("pool exhausted", "pool exhausted", "pool exhausted"), _log("deadlock found")]
    failures = [_failure(50 + i, log) for i, log in enumerate(logs)]
    docs = ["pool exhausted", "discount is null", "pool exhausted", "deadlock found"]
    examples = select_examples([0, 1, 2, 3], failures, distance, ([ASSERT] * 4, ["s"] * 4, docs))
    assert [(e.role, e.test_result_id) for e in examples] == [
        ("typical", 50), ("different", 51), ("informative", 53)]


@pytest.mark.parametrize("template", [
    "HTTP статус: {code}", 'response status="{code}"', "response_code={code}",
    '{{"status":{code},"error":"denied"}}', '"statusCode": {code}', "HTTP/1.1 {code} Denied",
])
def test_http_status_formats_keep_examples_apart(template: str) -> None:
    def distance(a: int, b: int) -> float:
        return 0.0 if a == b else 0.3

    texts = [template.format(code=code) for code in (401, 403)]
    failures = [_failure(60 + i, _log(text)) for i, text in enumerate(texts)]
    docs = [re.sub(r"\d+", "#", text) for text in texts]
    examples = select_examples([0, 1], failures, distance, ([ASSERT] * 2, ["s"] * 2, docs))
    assert len(examples) == 2, texts


def test_http_status_from_a_json_attachment_reaches_both_examples() -> None:
    import asyncio

    from alla_core.models.testops import AttachmentMeta
    from alla_core.services.log_extraction_service import LogExtractionConfig, LogExtractionService

    bodies = {1: b'{"status": 401, "error": "unauthorized"}',
              2: b'{"status": 403, "error": "forbidden"}',
              3: b'{"status": 401, "error": "unauthorized"}'}

    class Provider:
        async def get_attachments_for_test_result(self, test_id: int) -> list[AttachmentMeta]:
            return [AttachmentMeta(id=test_id, name="response.json", type="application/json")]

        async def get_attachment_content(self, attachment_id: int) -> bytes:
            return bodies[attachment_id]

    failures = [_failure(test_id) for test_id in bodies]
    asyncio.run(LogExtractionService(Provider(), LogExtractionConfig()).enrich_with_logs(failures))
    assert "HTTP статус: 403" in (failures[1].log_snippet or "")
    report = ClusteringService().cluster_failures(1, failures)
    cluster, = report.clusters  # одинаковый assertion и шаг — одна группа
    by_id = {failure.test_result_id: failure for failure in failures}
    codes = {code for example in cluster.examples for code in ("401", "403")
             if f"HTTP статус: {code}" in (by_id[example.test_result_id].log_snippet or "")}
    assert codes == {"401", "403"}


def test_durations_are_not_http_statuses() -> None:
    far = {frozenset({0, 1}): 0.3, frozenset({0, 2}): 0.2, frozenset({0, 3}): 0.2}

    def distance(a: int, b: int) -> float:
        return 0.0 if a == b else far.get(frozenset({a, b}), 0.5)

    # Повтор ошибки пула отличается только длительностью ответа — это не другая ошибка.
    texts = ["pool exhausted, response 401ms", "discount is null",
             "pool exhausted, response 403ms", "relation orders_v2 does not exist"]
    logs = [_log(texts[0]), _log(texts[1]), _log(texts[2], texts[2], texts[2]), _log(texts[3])]
    failures = [_failure(70 + i, log) for i, log in enumerate(logs)]
    docs = [re.sub(r"\d+", "#", text) for text in texts]
    examples = select_examples([0, 1, 2, 3], failures, distance, ([ASSERT] * 4, ["s"] * 4, docs))
    assert [(e.role, e.test_result_id) for e in examples] == [
        ("typical", 70), ("different", 71), ("informative", 73)]

"""Оценка ``prepare`` на синтетическом корпусе или на кассете команды — офлайн.

    python tests/eval/run_eval.py                       # dev, таблица
    python tests/eval/run_eval.py --details             # со списками склеек, потерь…
    python tests/eval/run_eval.py --heavy               # плюс большой прогон
    python tests/eval/run_eval.py --write-baseline      # обновить dev в tests/eval/baseline.json
    python tests/eval/run_eval.py --set holdout         # holdout: это просмотр, см. README
    python tests/eval/run_eval.py --cassette DIR --labels FILE   # только сводка
    python tests/eval/run_eval.py --cassette DIR                 # gates и размеры, без разметки
    python tests/eval/run_eval.py --analyses RUN_DIR [--labels FILE]  # чек-лист разборов

Настройки — значения скилла по умолчанию: переменные ``ALLURE_*`` из окружения не
учитываются, чтобы цифры сравнивались с базовой линией.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # запуск файлом: tests/ и scripts/ в sys.path
    _TESTS = Path(__file__).resolve().parents[1]
    for _path in (_TESTS, _TESTS.parent / "scripts"):
        if str(_path) not in sys.path:
            sys.path.insert(0, str(_path))

import pytest  # noqa: E402
from alla_core.models.clustering import ClusteringGateStats  # noqa: E402
from skill_fake_testops import TOKEN, LaunchFixture  # noqa: E402

from eval import corpus_dev, corpus_holdout  # noqa: E402
from eval.cassette import load_cassette, replay  # noqa: E402
from eval.corpus import Case, validate_labels  # noqa: E402
from eval.metrics import (  # noqa: E402
    KB_OFFER_KEYS,
    NEAR_KEYS,
    RETRY_KEYS,
    SIGNATURE_KEYS,
    ClusterView,
    combine,
    coverage,
    evaluate,
    evaluate_gate_pairs,
    evaluate_kb_auto,
    evaluate_kb_offers,
    evaluate_kb_version_change,
    evaluate_near_pairs,
    evaluate_retries,
    evaluate_signatures,
    kb_offer_records,
    summary,
)

BASELINE = Path(__file__).resolve().parent / "baseline.json"
SETS: dict[str, dict[str, Callable[[], Case]]] = {
    "dev": corpus_dev.CASES,
    "holdout": corpus_holdout.CASES,
}


@dataclass
class PreparedRun:
    """Что ``prepare`` показал модели: кластеры, задания, размер и время; gates кластеризации
    и стабильность сигнатуры (:func:`signature_log_stability`)."""

    run_dir: Path
    clusters: list[ClusterView]
    seconds: float
    triage: dict[str, Any]
    gates: ClusteringGateStats
    signature_log: dict[str, int]
    near_pairs: list[tuple[str, str, str]]
    # Модели ядра после prepare (кластеры по номеру, тесты с логами): по ним решается, что
    # next разобрал бы по записи без модели (``kb_auto``).
    cluster_models: dict[str, Any]
    tests_by_id: dict[int, Any]

    @property
    def max_task_chars(self) -> int:
        return max((len(cluster.task_text) for cluster in self.clusters), default=0)


def run_prepare(fixture: LaunchFixture, workdir: Path) -> PreparedRun:
    """Выполнить ``prepare`` на прогоне без сети, во временном проекте и папке скилла."""
    from alla_skill_lib import cli, pipeline, workspace
    from alla_skill_lib.cluster_task import select_log_source

    project = workdir / "project"
    (project / ".git").mkdir(parents=True)
    skill_dir = workdir / "skill"
    skill_dir.mkdir()
    reports = project / workspace.REPORTS_DIRNAME
    captured: list[Any] = []
    real_collect = pipeline.collect_launch

    async def collect(*args: Any, **kwargs: Any) -> Any:
        data = await real_collect(*args, **kwargs)
        captured.append(data)
        return data

    with pytest.MonkeyPatch.context() as monkeypatch:
        for key in list(os.environ):
            if key.startswith("ALLURE_"):
                monkeypatch.delenv(key)
        monkeypatch.setenv("ALLURE_ENDPOINT", "https://testops.example")
        monkeypatch.setenv("ALLURE_TOKEN", TOKEN)
        monkeypatch.setattr(workspace, "SKILL_DIR", skill_dir)
        monkeypatch.setattr(pipeline, "collect_launch", collect)
        # Кассета подменяет TestOps в памяти процесса: выгрузка — здесь, не фоновым заданием.
        monkeypatch.setattr(cli, "RUN_IN_BACKGROUND", False)
        out = io.StringIO()
        started = time.perf_counter()
        with replay(fixture), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli.cmd_prepare(int(fixture.launch["id"]), project, reports, fresh=True)
        seconds = time.perf_counter() - started
    if code != 0 or not captured:
        raise RuntimeError(f"prepare завершился с кодом {code}:\n{out.getvalue()}")

    data = captured[0]
    run_dir = Path(next(line for line in out.getvalue().splitlines()
                        if line.startswith("Папка разбора:")).split(":", 1)[1].strip())
    paths = workspace.RunPaths(run_dir)
    run = workspace.read_json(paths.run_json)
    tests_by_id = {test.test_result_id: test for test in data.triage.failed_tests}
    by_id = {cluster.cluster_id: cluster for cluster in
             (data.clustering.clusters if data.clustering else [])}
    views: list[ClusterView] = []
    stability = dict.fromkeys(("signature_log_clusters", "signature_log_unstable"), 0)
    for entry in run["clusters"]:
        cluster = by_id[entry["cluster_id"]]
        if entry.get("examples"):  # чьи данные видела модель (шаг 4: примеры кластера)
            visible = {example["test_result_id"] for example in entry["examples"]}
        else:
            visible = {cluster.representative_test_id} if cluster.representative_test_id else set()
            source = select_log_source(cluster, tests_by_id)
            if source is not None:
                visible.add(source.test_result_id)
        task = paths.cluster_task(entry["file_id"])
        text = task.read_text(encoding="utf-8") if task.is_file() else ""
        # Пути зависят от машины: без них размер задания сравним с базовой линией.
        for path, mark in ((workspace.ENTRYPOINT, "<alla_skill.py>"), (workdir.resolve(), "<tmp>"),
                           (workdir, "<tmp>")):
            text = text.replace(str(path), mark)
        views.append(ClusterView(
            file_id=entry["file_id"],
            members=tuple(cluster.member_test_ids),
            visible=tuple(sorted(visible)),
            task_text=text,
            signature=entry.get("signature"),
        ))
        if entry.get("signature"):
            logs, signatures = signature_log_stability(cluster, tests_by_id)
            stability["signature_log_clusters"] += logs > 1
            stability["signature_log_unstable"] += signatures > 1
    gates = data.clustering.gates if data.clustering else ClusteringGateStats()
    file_ids = {entry["cluster_id"]: entry["file_id"] for entry in run["clusters"]}
    near = [(file_ids[pair.cluster_a], file_ids[pair.cluster_b], pair.reason)
            for pair in (data.clustering.near_pairs if data.clustering else [])]
    models = {entry["file_id"]: by_id[entry["cluster_id"]] for entry in run["clusters"]}
    return PreparedRun(run_dir, views, seconds, run["triage"], gates, stability, near, models,
                       tests_by_id)


def signature_log_stability(cluster: Any, tests_by_id: dict[int, Any]) -> tuple[int, int]:
    """Сколько разных логов у участников кластера и сколько разных сигнатур он получил бы,
    будь представителем лог каждого из них: так между прогонами меняется лог одной проблемы.
    """
    from alla_skill_lib.signature import cluster_signature

    representative = tests_by_id.get(cluster.representative_test_id)
    if representative is None:
        return 0, 0
    logs = {(test.log_snippet, test.log_selection_truncated): None
            for test in (tests_by_id.get(test_id) for test_id in cluster.member_test_ids)
            if test is not None and test.log_snippet}
    signatures = set()
    for snippet, truncated in logs:
        tests = {**tests_by_id, representative.test_result_id: representative.model_copy(
            update={"log_snippet": snippet, "log_selection_truncated": truncated})}
        signatures.add(cluster_signature(cluster, tests))
    return len(logs), len(signatures)


def evaluate_fixture(fixture: LaunchFixture, labels: dict[str, Any] | None) -> dict[str, Any]:
    """Метрики прогона; без разметки — только размеры и gates (им разметка не нужна)."""
    if labels is None:
        with tempfile.TemporaryDirectory(prefix="alla-eval-") as tmp:
            prepared = run_prepare(fixture, Path(tmp))
        return {"tests": sum(len(cluster.members) for cluster in prepared.clusters),
                "clusters": len(prepared.clusters), "gates": prepared.gates.model_dump(),
                **evaluate_signatures(None, prepared.clusters), **prepared.signature_log,
                **evaluate_near_pairs(None, prepared.clusters, prepared.near_pairs),
                "max_task_chars": prepared.max_task_chars, "seconds": round(prepared.seconds, 2)}
    validate_labels(fixture, labels)
    with tempfile.TemporaryDirectory(prefix="alla-eval-") as tmp:
        prepared = run_prepare(fixture, Path(tmp))
        result = evaluate(labels, prepared.clusters)
        result.update(evaluate_signatures(labels, prepared.clusters))
        result.update(prepared.signature_log)
        result.update(evaluate_near_pairs(labels, prepared.clusters, prepared.near_pairs))
        result["gates"] = {**prepared.gates.model_dump(), **evaluate_gate_pairs(
            labels, prepared.gates.held_test_pairs, prepared.gates.override_test_pairs)}
        if labels.get("retries"):
            result.update(evaluate_retries(labels["retries"], prepared.triage))
        records = kb_offer_records(labels)
        if records:
            result.update(evaluate_kb_offers(labels, kb_offers(prepared, records)))
            result.update(evaluate_kb_version_change(
                labels, kb_offers(prepared, records, exact_only=True, as_remember=True),
                kb_offers(prepared, records, exact_only=True, previous_version=True,
                          as_remember=True)))
            result.update(evaluate_kb_auto(labels, kb_auto(prepared, records)))
    result["max_task_chars"] = prepared.max_task_chars
    result["seconds"] = round(prepared.seconds, 2)
    return result


def kb_offers(
    prepared: PreparedRun,
    records: list[dict[str, Any]],
    *,
    exact_only: bool = False,
    previous_version: bool = False,
    as_remember: bool = False,
    auto: bool = False,
) -> list[tuple[str, tuple[int, ...], set[str]]]:
    """Какие записи из ``records`` подходят каждому кластеру — по правилу ``prepare``.

    Записи подбираются после ``prepare`` по его ``evidence/NN.txt``, сигнатуре и тестам
    кластера (``match_cluster``), поэтому задания и остальные метрики от них не зависят.
    Сигнатура и тесты первой группы причины считаются подтверждёнными, как после
    ``remember``. ``exact_only`` — только точные узнавания; ``previous_version`` —
    подтверждённые сигнатуры записей прежней версии (обновление скилла сменило версию).
    ``as_remember`` — признак записи, как его принял бы ``remember``: строка разметки, если
    она есть в данных подтверждённого кластера, иначе признак кластера из ``prepare``
    (строки, которой нет в данных, ``remember`` не сохранит). ``auto`` — вместо подходящих
    записей причина той, по которой ``next`` разобрал бы кластер без модели (:func:`kb_auto`).
    """
    import re

    from alla_skill_lib import workspace
    from alla_skill_lib.kb import (
        CATEGORY_TO_KB,
        KBRecord,
        entry_tests,
        fingerprint_hits,
        key_of_test,
        match_cluster,
        store_fingerprint,
    )
    from alla_skill_lib.known_issues import auto_resolution
    from alla_skill_lib.signature import SIGNATURE_VERSION
    from alla_skill_lib.sources import load_registry

    paths = workspace.RunPaths(prepared.run_dir)
    run = workspace.read_json(paths.run_json)
    members = {cluster.file_id: cluster.members for cluster in prepared.clusters}
    signature_of = {test: entry.get("signature") for entry in run["clusters"]
                    for test in members[entry["file_id"]]}
    failed = {test["test_result_id"]: test for test in run["triage"]["failed_tests"]}
    kb_records = []
    cause_of: dict[str, str] = {}
    for record in records:
        entry_id = re.sub(r"[^a-z0-9]+", "_", record["cause"].lower()).strip("_")
        cause_of[entry_id] = record["cause"]
        confirmed = {str(signature) for signature in
                     {signature_of.get(test) for test in record["confirmed_tests"]} - {None}}
        logs = {str(entry["log_digest"]) for entry in run["clusters"] if entry.get("log_digest")
                and set(members[entry["file_id"]]) & set(record["confirmed_tests"])}
        if previous_version:
            confirmed = {f"v{SIGNATURE_VERSION - 1}:{signature.split(':', 1)[1]}"
                         for signature in confirmed}
        keys = {key_of_test(failed[test].get("full_name"), failed[test].get("test_case_id"))
                for test in record["confirmed_tests"] if test in failed}
        error_example = record["error_example"]
        if as_remember:
            own = next((entry for entry in run["clusters"]
                        if set(members[entry["file_id"]]) & set(record["confirmed_tests"])), None)
            evidence = paths.evidence(own["file_id"]) if own else None
            text = evidence.read_text(encoding="utf-8") if evidence and evidence.is_file() else ""
            if own and not fingerprint_hits(error_example, text):
                error_example = own.get("fingerprint") or error_example
        kb_records.append(KBRecord(
            id=entry_id, title=record["cause"],
            category=CATEGORY_TO_KB.get(str(record["category"]), "service"),
            description=record["cause"], resolution_steps=["-"],
            error_example=store_fingerprint(error_example),
            confirmed_signatures=sorted(confirmed),
            confirmed_tests=sorted(key for key in keys if key),
            confirmed_logs=sorted(logs),
        ))
    offers: list[tuple[str, tuple[int, ...], set[str]]] = []
    for entry in run["clusters"]:
        evidence = paths.evidence(entry["file_id"])
        text = evidence.read_text(encoding="utf-8") if evidence.is_file() else ""
        matches = match_cluster(kb_records, entry.get("signature"), text, entry_tests(run, entry))
        if auto:
            decision = None if entry.get("auto") else auto_resolution(
                matches, kb_records, prepared.cluster_models[entry["file_id"]],
                prepared.tests_by_id, load_registry(paths.sources(entry["file_id"])) or {},
                str(entry.get("log_digest")))
            offers.append((entry["file_id"], members[entry["file_id"]],
                           {cause_of[decision["id"]]} if decision else set()))
            continue
        offers.append((entry["file_id"], members[entry["file_id"]],
                       {cause_of[match["id"]] for match in matches
                        if not exact_only or match["origin"] == "exact"}))
    return offers


def kb_auto(
    prepared: PreparedRun,
    records: list[dict[str, Any]],
) -> list[tuple[str, tuple[int, ...], set[str]]]:
    """Причина записи, по которой ``next`` разобрал бы кластер без модели (пусто — разбирает
    модель): то же решение ``auto_resolution``, что в ``prepare``, на записях как после
    ``remember`` (:func:`kb_offers`, ``as_remember``)."""
    return kb_offers(prepared, records, as_remember=True, auto=True)


def evaluate_cases(
    names: Iterable[str] | None = None,
    sets: Iterable[str] = ("dev", "holdout"),
    heavy: bool = False,
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{набор: {сценарий: метрики}}``; heavy-сценарии — только при ``heavy``."""
    wanted = set(names) if names else None
    results: dict[str, dict[str, dict[str, Any]]] = {}
    for set_name in sets:
        for case_name, factory in SETS[set_name].items():
            if wanted is not None and case_name not in wanted:
                continue
            case = factory()
            if case.heavy and not heavy and wanted is None:
                continue
            results.setdefault(set_name, {})[case_name] = evaluate_fixture(
                case.fixture, case.labels)
    return results


def baseline_entry(result: dict[str, Any]) -> dict[str, Any]:
    """Метрики для базовой линии: без времени (оно зависит от машины)."""
    return {**summary(result), **retry_summary(result), **kb_offer_summary(result),
            **signature_summary(result), **near_summary(result), "gates": result["gates"],
            "max_task_chars": result["max_task_chars"]}


def signature_summary(result: dict[str, Any]) -> dict[str, int]:
    """Числа сигнатур; ``signature_shared_wrong`` — только с разметкой."""
    return {key: result[key] for key in SIGNATURE_KEYS if key in result}


def near_summary(result: dict[str, Any]) -> dict[str, int]:
    """Числа близких пар; ``near_pairs_same`` — только с разметкой."""
    return {key: result[key] for key in NEAR_KEYS if key in result}


def kb_offer_summary(result: dict[str, Any]) -> dict[str, int]:
    """Числа предложения записей — только у сценариев с причиной из нескольких групп."""
    return {key: result[key] for key in KB_OFFER_KEYS if key in result}


def retry_summary(result: dict[str, Any]) -> dict[str, int]:
    """Числа связи попыток — только у сценариев с разметкой ``retries``."""
    return {key: result[key] for key in RETRY_KEYS if key in result}


# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------

_COLUMNS = (
    ("сценарий", 28), ("тестов", 6), ("групп", 5), ("класт.", 6), ("precision", 9),
    ("recall", 6), ("скрыто", 6), ("потеря док.", 11), ("макс. задание", 13), ("сек", 5),
)


def _row(values: Iterable[object]) -> str:
    return "  ".join(str(value).ljust(width) for value, (_, width) in zip(values, _COLUMNS))


def _cells(name: str, result: dict[str, Any]) -> list[object]:
    lost = f"{result['evidence_lost']}/{result['evidence_total']} ({result['evidence_loss']:.0%})"
    return [name, result["tests"], result["groups"], result["clusters"],
            f"{result['precision']:.3f}", f"{result['recall']:.3f}", result["hidden_groups"],
            lost, result.get("max_task_chars", ""), result.get("seconds", "")]


def print_table(results: dict[str, dict[str, dict[str, Any]]]) -> None:
    for set_name, cases in results.items():
        print(f"\n## {set_name}")
        print(_row(title for title, _ in _COLUMNS))
        for case_name, result in cases.items():
            print(_row(_cells(case_name, result)))
        print(_row(_cells("ИТОГО", {**combine(cases.values()),
                                     "max_task_chars": "", "seconds": ""})))
        for case_name, result in cases.items():
            if "retry_links" in result:
                print(f"повторы {case_name}: {_retry_cells(result)}")
        for case_name, result in cases.items():
            if "kb_offers" in result:
                print(f"записи базы знаний {case_name}: предложены своей причине "
                      f"{result['kb_offers_found']}/{result['kb_offers']}, чужой "
                      f"{result['kb_offers_wrong']}; точно {result['kb_exact']}, после смены "
                      f"версии сигнатуры точно {result['kb_exact_kept']}, чужой "
                      f"{result['kb_exact_wrong_after']}; без модели {result['kb_auto']}, из них "
                      f"с другой проблемой в группе {result['kb_auto_wrong']}")
        for case_name, result in cases.items():
            if result["signature_shared"] or result["signature_log_unstable"]:
                print(f"сигнатуры {case_name}: одна на два кластера {result['signature_shared']} "
                      f"(у разных проблем {result['signature_shared_wrong']}); меняется с "
                      f"логом участника {result['signature_log_unstable']} из "
                      f"{result['signature_log_clusters']}")
        for case_name, result in cases.items():
            if result["near_pairs"]:
                print(f"близкие пары {case_name}: {result['near_pairs']}, из них одна "
                      f"проблема {result['near_pairs_same']}")


def _retry_cells(result: dict[str, Any]) -> str:
    return (
        f"связано {result['retry_links_found']}/{result['retry_links']}, "
        f"лишних {result['retry_links_wrong']}; «та же ошибка» "
        f"{result['retry_same_found']}/{result['retry_same']}; прошли после повтора "
        f"{result['passed_after_retry_found']}/{result['passed_after_retry']}, "
        f"лишних {result['passed_after_retry_wrong']}"
    )


def print_details(results: dict[str, dict[str, dict[str, Any]]]) -> None:
    for set_name, cases in results.items():
        for case_name, result in cases.items():
            lines: list[str] = []
            lines += [f"  склеены: {a} + {b}" for a, b in result["merged"]]
            lines += [f"  раздроблена: {group} → кластеры {', '.join(ids)}"
                      for group, ids in result["split"].items()]
            lines += [f"  скрыта: {group}" for group in result["hidden"]]
            lines += [f"  потеряна ({item['reason']}): {item['group']}: {item['line']}"
                      for item in result["lost"]]
            lines += [f"  повторы: {problem}" for problem in result.get("retry_problems", [])]
            lines += [f"  база знаний: {problem}" for problem in result.get("kb_offer_problems", [])]
            lines += [f"  база знаний: {problem}"
                      for problem in result.get("kb_version_problems", [])]
            lines += [f"  база знаний: {problem}" for problem in result.get("kb_auto_problems", [])]
            lines += [f"  сигнатура: {problem}" for problem in result.get("signature_problems", [])]
            lines += [f"  близкие: {problem}" for problem in result.get("near_problems", [])]
            decided = {key: value for key, value in result["gates"].items() if value}
            if set(decided) - {"pairs", "pairs_in_one_problem"}:
                lines.append("  gates: " + ", ".join(f"{key} {value}"
                                                     for key, value in decided.items()))
            if result["unclustered"] or result["unlabeled"]:
                lines.append(f"  вне кластеров: {result['unclustered']}, "
                             f"без разметки: {result['unlabeled']}")
            if lines:
                print(f"\n{set_name}/{case_name}:")
                print("\n".join(lines))


def analyses_checklist(run_dir: Path, labels: dict[str, Any] | None) -> str:
    """Чек-лист для ручной сверки: эталонная причина и категория ↔ разбор модели."""
    from alla_skill_lib.analysis_format import parse_analysis
    from alla_skill_lib.workspace import RunPaths, read_json

    paths = RunPaths(run_dir)
    run = read_json(paths.run_json)
    members = {cluster["cluster_id"]: cluster["member_test_ids"]
               for cluster in (run.get("clustering") or {}).get("clusters", [])}
    group_of = {test: group for group in (labels or {"groups": []})["groups"]
                for test in group["tests"]}
    lines = [f"# Сверка разборов: {run_dir}", "",
             "Отметьте вручную: верна ли причина и категория. Совпадение цитат правильность "
             "причины не доказывает.", ""]
    for position, entry in enumerate(run["clusters"], start=1):
        tests = members.get(entry["cluster_id"], [])
        lines.append(f"## Проблема {position}: {entry['label']} (тестов {len(tests)})")
        if labels is not None:
            counts: dict[str, int] = {}
            for test in tests:
                group_id = group_of[test]["id"] if test in group_of else "без разметки"
                counts[group_id] = counts.get(group_id, 0) + 1
            for group_id, count in counts.items():
                group = next((g for g in labels["groups"] if g["id"] == group_id), {})
                lines.append(f"- эталон: {group_id} × {count} — причина "
                             f"{group.get('cause') or 'неизвестна'}, категория "
                             f"{group.get('category', '—')}")
        path = paths.analysis(entry["file_id"])
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            analysis = parse_analysis(path.read_text(encoding="utf-8"))
            lines.append(f"- разбор: {analysis.category or 'категория не распознана'} — "
                         f"{analysis.cause_reason or '(причина пуста)'}")
        else:
            lines.append("- разбор: нет")
        lines += ["- [ ] причина верна   - [ ] категория верна", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--set", choices=("dev", "holdout", "all"), default="dev",
                        help="набор сценариев; holdout и all — просмотр holdout (README)")
    parser.add_argument("--case", action="append", help="только этот сценарий (можно повторять)")
    parser.add_argument("--heavy", action="store_true", help="включить большой прогон")
    parser.add_argument("--details", action="store_true",
                        help="списки склеенных, раздробленных, скрытых групп и потерянных строк")
    parser.add_argument("--json", type=Path, help="записать полный результат в JSON")
    parser.add_argument("--write-baseline", action="store_true",
                        help=f"записать в {BASELINE.name} наборы из --set (с большим прогоном), "
                             "остальные оставить как есть")
    parser.add_argument("--cassette", type=Path,
                        help="кассета команды: только сводка из чисел (без --labels — gates и размеры)")
    parser.add_argument("--labels", type=Path, help="labels.json к кассете или разбору")
    parser.add_argument("--analyses", type=Path, help="папка разбора: чек-лист для сверки")
    args = parser.parse_args(argv)

    labels = json.loads(args.labels.read_text(encoding="utf-8")) if args.labels else None
    if args.analyses:
        print(analyses_checklist(args.analyses, labels))
        return 0
    if args.cassette:
        try:
            result = evaluate_fixture(load_cassette(args.cassette), labels)
        except ValueError as exc:
            print(f"Разметка не подходит к кассете: {exc}", file=sys.stderr)
            return 2
        if labels is None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({**summary(result), **retry_summary(result), **coverage(result),
                          **signature_summary(result), **near_summary(result), "gates": result["gates"],
                          "max_task_chars": result["max_task_chars"],
                          "seconds": result["seconds"]}, ensure_ascii=False, indent=2))
        if result["unclustered"] or result["unlabeled"]:
            print("Внимание: разметка не совпадает с активными падениями прогона — "
                  "цифры выше неполные.", file=sys.stderr)
            return 1
        return 0

    sets = ("dev", "holdout") if args.set == "all" else (args.set,)
    results = evaluate_cases(args.case, sets, heavy=args.heavy or args.write_baseline)
    print_table(results)
    if args.details:
        print_details(results)
    if args.json:
        args.json.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    if args.write_baseline:
        if args.case:
            parser.error("--write-baseline считается по всем сценариям набора")
        write_baseline(results, sets)
        print(f"\nБазовая линия записана: {BASELINE}")
    return 0


def write_baseline(results: dict[str, dict[str, dict[str, Any]]], sets: Iterable[str]) -> None:
    """Переписать в базовой линии наборы ``sets``; остальные остаются как были — так
    базовая линия holdout меняется только отдельной командой (и отдельным коммитом)."""
    baseline: dict[str, Any] = (json.loads(BASELINE.read_text(encoding="utf-8"))
                                if BASELINE.is_file() else {})
    for set_name in sets:
        baseline.pop(set_name, None)
        if results.get(set_name):  # пустой набор (holdout между ротациями) — без раздела
            baseline[set_name] = {case_name: baseline_entry(result)
                                  for case_name, result in results[set_name].items()}
    BASELINE.write_text(json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

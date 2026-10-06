"""Оценка ``prepare`` на синтетическом корпусе или на кассете команды — офлайн.

    python tests/eval/run_eval.py                       # dev и holdout, таблица
    python tests/eval/run_eval.py --set dev --details   # со списками склеек, потерь…
    python tests/eval/run_eval.py --heavy               # плюс большой прогон
    python tests/eval/run_eval.py --write-baseline      # обновить tests/eval/baseline.json
    python tests/eval/run_eval.py --cassette DIR --labels FILE   # только сводка
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
from skill_fake_testops import TOKEN, LaunchFixture  # noqa: E402

from eval import corpus_dev, corpus_holdout  # noqa: E402
from eval.cassette import load_cassette, replay  # noqa: E402
from eval.corpus import Case, validate_labels  # noqa: E402
from eval.metrics import (  # noqa: E402
    KB_OFFER_KEYS,
    RETRY_KEYS,
    ClusterView,
    combine,
    coverage,
    evaluate,
    evaluate_kb_offers,
    evaluate_retries,
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
    """Что ``prepare`` показал модели: кластеры, задания, размер и время."""

    run_dir: Path
    clusters: list[ClusterView]
    seconds: float
    triage: dict[str, Any]

    @property
    def max_task_chars(self) -> int:
        return max((len(cluster.task_text) for cluster in self.clusters), default=0)


def run_prepare(fixture: LaunchFixture, workdir: Path) -> PreparedRun:
    """Выполнить ``prepare`` на прогоне без сети, во временном проекте и папке скилла."""
    from alla_core.services import log_extraction_service
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
        # В venv скилла нет python-magic: тип вложения — из метаданных TestOps.
        monkeypatch.setattr(log_extraction_service, "_MAGIC_AVAILABLE", False)
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
        ))
    return PreparedRun(run_dir, views, seconds, run["triage"])


def evaluate_fixture(fixture: LaunchFixture, labels: dict[str, Any]) -> dict[str, Any]:
    validate_labels(fixture, labels)
    with tempfile.TemporaryDirectory(prefix="alla-eval-") as tmp:
        prepared = run_prepare(fixture, Path(tmp))
        result = evaluate(labels, prepared.clusters)
        if labels.get("retries"):
            result.update(evaluate_retries(labels["retries"], prepared.triage))
        records = kb_offer_records(labels)
        if records:
            result.update(evaluate_kb_offers(labels, kb_offers(prepared, records)))
    result["max_task_chars"] = prepared.max_task_chars
    result["seconds"] = round(prepared.seconds, 2)
    return result


def kb_offers(
    prepared: PreparedRun,
    records: list[dict[str, Any]],
) -> list[tuple[str, tuple[int, ...], set[str]]]:
    """Какие записи из ``records`` подходят каждому кластеру — по правилу ``prepare``.

    Записи подбираются после ``prepare`` по его ``evidence/NN.txt`` и сигнатуре кластера
    (``match_cluster``), поэтому задания и остальные метрики от них не зависят. Сигнатура
    первой группы причины считается подтверждённой, как после ``remember``.
    """
    import re

    from alla_skill_lib import workspace
    from alla_skill_lib.kb import CATEGORY_TO_KB, KBRecord, match_cluster, store_fingerprint

    paths = workspace.RunPaths(prepared.run_dir)
    run = workspace.read_json(paths.run_json)
    members = {cluster.file_id: cluster.members for cluster in prepared.clusters}
    signature_of = {test: entry.get("signature") for entry in run["clusters"]
                    for test in members[entry["file_id"]]}
    kb_records = []
    cause_of: dict[str, str] = {}
    for record in records:
        entry_id = re.sub(r"[^a-z0-9]+", "_", record["cause"].lower()).strip("_")
        cause_of[entry_id] = record["cause"]
        confirmed = {signature_of.get(test) for test in record["confirmed_tests"]} - {None}
        kb_records.append(KBRecord(
            id=entry_id, title=record["cause"],
            category=CATEGORY_TO_KB.get(str(record["category"]), "service"),
            description="", resolution_steps=["-"],
            error_example=store_fingerprint(record["error_example"]),
            confirmed_signatures=sorted(str(signature) for signature in confirmed),
        ))
    offers: list[tuple[str, tuple[int, ...], set[str]]] = []
    for entry in run["clusters"]:
        evidence = paths.evidence(entry["file_id"])
        text = evidence.read_text(encoding="utf-8") if evidence.is_file() else ""
        matches = match_cluster(kb_records, entry.get("signature"), text)
        offers.append((entry["file_id"], members[entry["file_id"]],
                       {cause_of[match["id"]] for match in matches}))
    return offers


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
            "max_task_chars": result["max_task_chars"]}


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
                      f"{result['kb_offers_wrong']}")


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
    parser.add_argument("--set", choices=("dev", "holdout", "all"), default="all")
    parser.add_argument("--case", action="append", help="только этот сценарий (можно повторять)")
    parser.add_argument("--heavy", action="store_true", help="включить большой прогон")
    parser.add_argument("--details", action="store_true",
                        help="списки склеенных, раздробленных, скрытых групп и потерянных строк")
    parser.add_argument("--json", type=Path, help="записать полный результат в JSON")
    parser.add_argument("--write-baseline", action="store_true",
                        help=f"записать базовую линию {BASELINE.name} (dev и holdout, с большим прогоном)")
    parser.add_argument("--cassette", type=Path, help="кассета команды (только сводка)")
    parser.add_argument("--labels", type=Path, help="labels.json к кассете или разбору")
    parser.add_argument("--analyses", type=Path, help="папка разбора: чек-лист для сверки")
    args = parser.parse_args(argv)

    labels = json.loads(args.labels.read_text(encoding="utf-8")) if args.labels else None
    if args.analyses:
        print(analyses_checklist(args.analyses, labels))
        return 0
    if args.cassette:
        if labels is None:
            parser.error("--cassette требует --labels")
        try:
            result = evaluate_fixture(load_cassette(args.cassette), labels)
        except ValueError as exc:
            print(f"Разметка не подходит к кассете: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({**summary(result), **retry_summary(result), **coverage(result),
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
        if args.case or args.set != "all":
            parser.error("--write-baseline считается по всем сценариям dev и holdout")
        write_baseline(results)
        print(f"\nБазовая линия записана: {BASELINE}")
    return 0


def write_baseline(results: dict[str, dict[str, dict[str, Any]]]) -> None:
    baseline = {set_name: {case_name: baseline_entry(result)
                           for case_name, result in cases.items()}
                for set_name, cases in results.items()}
    BASELINE.write_text(json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

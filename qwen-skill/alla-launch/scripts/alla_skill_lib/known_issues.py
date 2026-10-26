"""Известные проблемы: разборы, подтверждённые записью базы знаний, и их группы.

Одна причина часто даёт разные симптомы, и кластеризация разносит их по разным
проблемам. Если модель в нескольких разборах приняла одну запись базы знаний,
отчёт показывает их вместе — как одну известную проблему, ничего не теряя:
номера, разборы и правки остаются у своих проблем.

Ссылка разбора на запись (``БАЗА ЗНАНИЙ: <id>``) считается, только пока запись
подходит кластеру по текущим файлам ``alla-kb/`` — тем же правилом, что подбор
записей в ``prepare`` (``kb.record_matches``). Поэтому после ``reject`` проблема
выходит из группы при следующей сборке отчёта. Всё вычисляется заново при каждой
сборке и нигде не хранится.

Точно узнанную проблему (сигнатуру подтвердил инженер) скрипт разбирает сам, без модели:
``prepare`` выбирает запись (``auto_resolution``), а ``next`` пишет разбор из её текущего
текста или снимает его, когда запись перестала подходить (``sync_known_auto``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alla_core.models.clustering import FailureCluster
from alla_core.models.testops import FailedTestSummary
from alla_skill_lib import workspace as ws
from alla_skill_lib.analysis_format import MIN_QUOTE_CHARS, ClusterAnalysis
from alla_skill_lib.kb import (
    KB_TO_CATEGORY,
    KBRecord,
    ProjectKB,
    cluster_evidence,
    cluster_signature,
    entry_tests,
    fingerprint_hits,
    fingerprint_lines,
    kb_label,
    key_of_test,
    match_cluster,
    normalize_fp,
    record_matches,
    record_rejected,
)
from alla_skill_lib.signature import cluster_log_errors, log_error_lines
from alla_skill_lib.sources import IMAGE_KIND, Registry, check_entry_analysis, example_blocks


@dataclass(frozen=True)
class KnownRecord:
    """Запись базы знаний, на которую ссылается разбор (то, что нужно отчёту и сводке)."""

    kb_dir: str
    id: str
    title: str
    category: str | None  # категория скилла: тест | приложение | окружение | данные
    description: str
    first_step: str
    module: str = ""
    # False — папку базы знаний не найти: ссылка взята из разбора и снимка prepare как есть.
    verified: bool = True
    created: str = ""  # дата записи (``created.date``)

    @property
    def key(self) -> tuple[str, str]:
        return (self.kb_dir, self.id)


@dataclass
class KnownIssue:
    """Одна известная проблема у нескольких проблем отчёта."""

    record: KnownRecord
    file_ids: list[str]
    size: int  # тестов во всех проблемах группы


@dataclass
class KnownIssues:
    """Подтверждённые ссылки разборов на записи и группы из них."""

    refs: dict[str, KnownRecord] = field(default_factory=dict)
    groups: list[KnownIssue] = field(default_factory=list)
    # Разбор ссылается на запись, но называет другую категорию причины.
    mismatched: dict[str, KnownRecord] = field(default_factory=dict)
    # Разбор опирается на запись, которую пользователь потом отверг для этой ошибки
    # (``reject``): текст разбора остался прежним, его причина не подтверждена.
    rejected: dict[str, KnownRecord] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # В прогоне записи из нескольких папок базы знаний (модулей): модуль надо называть.
    several_modules: bool = False
    # Разобраны скриптом по точной записи, без модели (``sync_known_auto``).
    auto: set[str] = field(default_factory=set)

    def group_of(self, file_id: str) -> KnownIssue | None:
        return next((group for group in self.groups if file_id in group.file_ids), None)


def known_issues(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    flagged: set[str],
    paths: ws.RunPaths,
    auto: Iterable[str] = (),
) -> KnownIssues:
    """Подтвердить ссылки разборов на записи базы знаний и собрать группы.

    Проблема входит в группу записи, если разбор принят, ссылается на запись,
    запись подходит кластеру сейчас, категория разбора совпадает с категорией
    записи и примеры не названы «разными проблемами». Группа — от двух проблем.
    ``auto`` — проблемы, которые скрипт разобрал по записи (``sync_known_auto``).
    """
    result = KnownIssues()
    cache: dict[tuple[str, str], KBRecord | None] = {}
    unreachable: set[str] = set()
    for entry in run["clusters"]:
        file_id = entry["file_id"]
        analysis = analyses.get(file_id)
        if analysis is None or file_id in flagged or not analysis.kb_ref:
            continue
        kb_dir = str(entry.get("kb_dir") or "")
        if not kb_dir:
            continue
        directory = Path(kb_dir)
        if not directory.is_dir():
            # Разбор открыт не там, где готовился: проверить нечем — ссылка как в разборе.
            unreachable.add(kb_dir)
            snapshot = next((m for m in entry.get("kb", []) if m.get("id") == analysis.kb_ref), None)
            result.refs[file_id] = _from_snapshot(kb_dir, analysis.kb_ref, snapshot, entry)
            continue
        record = _load(cache, directory, analysis.kb_ref, run, result.notes)
        if record is None:
            continue
        if _rejected(record, run, entry, paths):
            result.rejected[file_id] = _from_record(kb_dir, record, entry)
            continue
        if not _matches(record, run, entry, paths):
            continue
        known = _from_record(kb_dir, record, entry)
        if analysis.category != known.category:
            result.mismatched[file_id] = known
        else:
            result.refs[file_id] = known

    project_root = Path(str(run.get("project_root") or "."))
    for kb_dir in sorted(unreachable):
        result.notes.append(
            f"База знаний {kb_label(project_root, Path(kb_dir))} недоступна — "
            "известные проблемы по ней не сгруппированы."
        )
    result.several_modules = len({ref.kb_dir for ref in (*result.refs.values(), *result.mismatched.values())}) > 1
    result.groups = _groups(run, analyses, result.refs)
    result.auto = {file_id for file_id in auto if file_id in result.refs}
    return result


def _groups(
    run: dict[str, Any],
    analyses: dict[str, ClusterAnalysis],
    refs: dict[str, KnownRecord],
) -> list[KnownIssue]:
    sizes = {entry["file_id"]: int(entry["member_count"]) for entry in run["clusters"]}
    members: dict[tuple[str, str], list[str]] = {}
    for file_id, ref in refs.items():
        # Неоднородная группа (шаг 4) не объединяется: одна запись на смешанные тесты.
        if ref.verified and analyses[file_id].consistency_kind != "different":
            members.setdefault(ref.key, []).append(file_id)
    groups = [
        KnownIssue(
            record=refs[file_ids[0]],
            file_ids=sorted(file_ids, key=int),
            size=sum(sizes[file_id] for file_id in file_ids),
        )
        for file_ids in members.values()
        if len(file_ids) > 1
    ]
    return sorted(groups, key=lambda group: (-group.size, int(group.file_ids[0])))


def _load(
    cache: dict[tuple[str, str], KBRecord | None],
    directory: Path,
    entry_id: str,
    run: dict[str, Any],
    notes: list[str],
) -> KBRecord | None:
    key = (str(directory), entry_id)
    if key not in cache:
        kb = ProjectKB(directory, kb_label(Path(str(run.get("project_root") or ".")), directory))
        try:
            cache[key] = kb.get(entry_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            cache[key] = None
            notes.append(f"База знаний: запись {kb.label}/{entry_id}.json не читается — {exc}")
    return cache[key]


def current_kb_matches(run: dict[str, Any], paths: ws.RunPaths) -> dict[str, list[dict[str, Any]]]:
    """Записи базы знаний, которые подходят кластеру **сейчас**: file_id → снимки.

    Тот же ``match_cluster``, что в ``prepare``, но по текущим файлам: запись, отвергнутая
    для кластера (``reject``) после разбора, — даже если разбор на неё не ссылался — уже не
    подходит. Снимок дополнен ``kb_dir`` и ``kb_label``: id уникален только внутри папки
    базы знаний модуля. Папки нет (разбор открыт не там) — записей нет: проверить нечем.
    """
    project_root = Path(str(run.get("project_root") or "."))
    cache: dict[str, list[KBRecord]] = {}
    result: dict[str, list[dict[str, Any]]] = {}
    for entry in run["clusters"]:
        kb_dir = str(entry.get("kb_dir") or "")
        if entry.get("auto") or not kb_dir or not Path(kb_dir).is_dir():
            continue
        if kb_dir not in cache:
            cache[kb_dir], _warnings = ProjectKB(
                Path(kb_dir), kb_label(project_root, Path(kb_dir))).load()
        evidence = paths.evidence(entry["file_id"])
        text = ws.read_text(evidence) if evidence.is_file() else ""
        label = kb_label(project_root, Path(kb_dir))
        result[entry["file_id"]] = [
            {**match, "kb_dir": kb_dir, "kb_label": label}
            for match in match_cluster(cache[kb_dir], entry.get("signature"), text,
                                       entry_tests(run, entry))]
    return result


def _evidence(entry: dict[str, Any], paths: ws.RunPaths) -> str:
    """Данные кластера для признака; без доказательств (кластер auto) — пусто."""
    evidence = paths.evidence(entry["file_id"])
    return ws.read_text(evidence) if evidence.is_file() else ""


def _rejected(record: KBRecord, run: dict[str, Any], entry: dict[str, Any],
              paths: ws.RunPaths) -> bool:
    """Пользователь отверг запись для этой ошибки (по сигнатуре или, после смены версии, по тесту)."""
    return record_rejected(record, entry.get("signature"), entry_tests(run, entry),
                           hits=fingerprint_hits(record.error_example, _evidence(entry, paths)))


def _matches(record: KBRecord, run: dict[str, Any], entry: dict[str, Any],
             paths: ws.RunPaths) -> bool:
    """Запись подходит кластеру сейчас: не отвергнута и узнаётся точно или по признаку."""
    # Без доказательств — только точное совпадение сигнатуры.
    return record_matches(record, entry.get("signature"), normalize_fp(_evidence(entry, paths)),
                          entry_tests(run, entry)) is not None


def _from_record(kb_dir: str, record: KBRecord, entry: dict[str, Any]) -> KnownRecord:
    return KnownRecord(
        kb_dir=kb_dir,
        id=record.id,
        title=record.title,
        category=KB_TO_CATEGORY.get(record.category),
        description=" ".join(record.description.split()),
        first_step=next((step.strip() for step in record.resolution_steps if step.strip()), ""),
        module=str(entry.get("module") or ""),
        created=str(record.created.get("date") or ""),
    )


def _from_snapshot(
    kb_dir: str,
    entry_id: str,
    snapshot: dict[str, Any] | None,
    entry: dict[str, Any],
) -> KnownRecord:
    snapshot = snapshot or {}
    steps = [str(step) for step in snapshot.get("steps") or []]
    return KnownRecord(
        kb_dir=kb_dir,
        id=entry_id,
        title=str(snapshot.get("title") or entry_id),
        category=snapshot.get("category"),
        description=" ".join(str(snapshot.get("description") or "").split()),
        first_step=next((step.strip() for step in steps if step.strip()), ""),
        module=str(entry.get("module") or ""),
        verified=False,
    )


# ---------------------------------------------------------------------------
# Разбор точно узнанной проблемы без модели
# ---------------------------------------------------------------------------

# Ключ ``state.json``: file_id → хэш разбора, который записал скрипт. По нему ``next`` отличает
# свой текст от разбора модели, написанного после того, как запись перестала подходить.
KB_AUTO_STATE = "kb_auto"
KB_AUTO_QUOTE_CHARS = 300
KB_AUTO_NOT_PROPOSED = (
    "известная проблема из базы знаний — её разбор взят из записи, что делать — в её рецепте"
)
KB_AUTO_INVALID_NOTE = (
    "Проблема {number}: разбор по записи базы знаний «{entry_id}» не прошёл проверку — её "
    "разбирает агент."
)


def auto_resolution(
    kb_matches: list[dict[str, Any]],
    records: Iterable[KBRecord],
    cluster: FailureCluster,
    tests_by_id: dict[int, FailedTestSummary],
    sources: Registry,
    log_digest: str,
) -> dict[str, Any] | None:
    """Разобрать ли проблему по записи базы знаний без модели: ``{"id", "quote", "signatures"}``
    или ``None``.

    Только точное узнавание (сигнатуру подтвердил инженер) и ровно одна такая запись; у
    записи есть причина и рецепт; инженер подтверждал её с тем же логом (``log_digest`` — в
    ``confirmed_logs``): сигнатура ошибки, которая сама называет причину, лог не берёт, и NPE
    за исключением клиента, подтверждённым для пула БД, иначе получила бы причину пула (ревью
    Codex). Каждый тест группы, и представитель тоже, — точное узнавание этой записью по
    своему тесту (после смены версии сигнатуры кластер узнаётся и по тесту другого участника —
    ревью Codex), с теми же строками-ошибками своего лога, что у лога кластера: иначе в группе
    может быть другая ошибка, и однородность проверяет модель. Все тесты, а не только примеры
    задания: примеров до трёх, и тест другой проблемы в них может не попасть. Сигнатуры тестов
    (``signatures``) ``next`` сверяет с текущей записью: отказ по любой снимает разбор. Цитата
    — строка куска данных с признаком записи, иначе первая строка сообщения или трейса; текст
    разбора проходит обычную проверку. Не вышло — ``None``: разбирает модель с подсказкой.
    """
    exact = [match["id"] for match in kb_matches if match.get("origin") == "exact"]
    if len(exact) != 1:
        return None
    record = next((item for item in records if item.id == exact[0]), None)
    if record is None or not _record_steps(record) or log_digest not in record.confirmed_logs:
        return None
    shown = cluster_log_errors(cluster, tests_by_id)
    signatures: set[str] = set()
    for test_id in cluster.member_test_ids:
        signature = _member_signature(record, cluster, tests_by_id, test_id, shown)
        if signature is None:
            return None
        signatures.add(signature)
    quote = _quote(record, sources)
    if quote is None:
        return None
    return {"id": record.id, "quote": list(quote), "signatures": sorted(signatures)}


def _member_signature(
    record: KBRecord,
    cluster: FailureCluster,
    tests_by_id: dict[int, FailedTestSummary],
    test_id: int,
    shown: frozenset[str],
) -> str | None:
    """Сигнатура теста группы, если запись узнаёт его точно, как если бы он был представителем
    кластера, и строки-ошибки его лога — те же, что у лога кластера (``shown``), если свой лог
    у него есть; иначе ``None``."""
    test = tests_by_id.get(test_id)
    if test is None:
        return None
    alone = cluster.model_copy(update={"representative_test_id": test_id})
    evidence = "\n".join(part for part in cluster_evidence(alone, tests_by_id) if part)
    key = key_of_test(test.full_name, test.test_case_id)
    signature = cluster_signature(alone, tests_by_id)
    if signature is None or record_matches(record, signature, normalize_fp(evidence),
                                           [key] if key else []) != "exact":
        return None
    # Сигнатура ошибки, которая сама называет причину, лог не берёт: разные ошибки сервиса в
    # логах тестов (пул БД у одного, NPE у другого) она не различает — это видно только по
    # логам, и однородность группы проверяет модель. Фон или причина — по паре не отличить.
    # Тест без своего лога ничего не опровергает.
    own = log_error_lines(test)
    return signature if not own or own == shown else None


def _quote(record: KBRecord, sources: Registry) -> tuple[str, str] | None:
    """Строка данных для «НАБЛЮДЕНИЯ»: с признаком записи, иначе первая строка ошибки."""
    ordered = sorted(
        ((source_id, source) for source_id, source in sources.items()
         if source.get("kind") != IMAGE_KIND),
        key=lambda item: int(item[0][1:]) if item[0][1:].isdigit() else 0)
    for wanted in (normalize_fp(line) for line in fingerprint_lines(record.error_example)):
        for source_id, source in ordered:
            for line in str(source.get("text") or "").splitlines():
                if wanted and wanted in normalize_fp(line) and _significant(line):
                    return source_id, _quote_text(line)
    for source_id, source in ordered:
        if source.get("kind") in ("message", "trace"):
            line = next((line for line in str(source.get("text") or "").splitlines()
                         if _significant(line)), "")
            if line:
                return source_id, _quote_text(line)
    return None


def _significant(line: str) -> bool:
    return sum(char.isalnum() for char in line) >= MIN_QUOTE_CHARS


def _quote_text(line: str) -> str:
    """Цитата одной строкой: без «ёлочек» (они закрыли бы её раньше) и не длиннее лимита.

    Сверка цитаты не различает вид кавычек, а начало строки — её часть: обрезанная по границе
    слова цитата находится в куске так же.
    """
    text = " ".join(line.replace("«", '"').replace("»", '"').split())
    if len(text) <= KB_AUTO_QUOTE_CHARS:
        return text
    cut = text[:KB_AUTO_QUOTE_CHARS]
    return cut[: cut.rfind(" ")].rstrip() if " " in cut else cut


def _record_steps(record: KBRecord) -> list[str]:
    return [" ".join(step.split()) for step in record.resolution_steps if step.strip()]


def known_issue_analysis(record: KBRecord, quote: tuple[str, str], *, examples: int = 1) -> str:
    """Разбор проблемы по записи базы знаний — в обычном формате, его проверяет ``next``."""
    title = " ".join(record.title.split()) or record.id
    cause = " ".join(record.description.split()) or title
    source_id, line = quote
    return "\n".join([
        # Первое предложение — в кратком разборе: короткое, без пояснения, откуда разбор.
        f"ЧТО СЛОМАЛОСЬ: Известная проблема из базы знаний: «{title}». Эту ошибку уже разобрал "
        "и подтвердил инженер, разбор взят из записи.",
        "",
        f"ПРИЧИНА: {KB_TO_CATEGORY[record.category]} — {cause}",
        "",
        "НАБЛЮДЕНИЯ:",
        f"- [{source_id}] «{line}»",
        "",
        "НЕ ХВАТАЕТ: нет",
        "",
        *(["СОГЛАСОВАННОСТЬ: одна причина — каждый пример узнан записью базы знаний точно", ""]
          if examples > 1 else []),
        "КАК ИСПРАВИТЬ:",
        *(f"{number}. {step}" for number, step in enumerate(_record_steps(record), start=1)),
        "",
        f"БАЗА ЗНАНИЙ: {record.id}",
    ]) + "\n"


def sync_known_auto(
    paths: ws.RunPaths,
    run: dict[str, Any],
    state: dict[str, Any],
) -> tuple[set[str], list[str]]:
    """Разборы известных проблем по текущим файлам базы знаний: записать, обновить или снять.

    Запись подходит кластеру точно — разбор пишется из её текущего текста (после
    ``remember --entry`` рецепт обновится). Перестала подходить (``reject``, запись удалена
    или ошибку переназначили другой) — свой разбор удаляется, и проблему разбирает модель.
    Разбор, который написал не скрипт (хэш другой), не трогается. Возвращает номера проблем,
    разобранных по базе знаний, и замечания для отчёта; ``state`` меняется на месте.
    """
    written: dict[str, str] = dict(state.get(KB_AUTO_STATE) or {})
    project_root = Path(str(run.get("project_root") or "."))
    active: set[str] = set()
    notes: list[str] = []
    for entry in run["clusters"]:
        auto = entry.get("kb_auto")
        if not auto:
            continue
        file_id = entry["file_id"]
        path = paths.analysis(file_id)
        current = ws.read_text(path) if path.is_file() else ""
        if current.strip() and _digest(current) != written.get(file_id):
            continue  # разбор после отката пишет модель
        record = _exact_now(run, entry, paths, str(auto["id"]))
        text = (known_issue_analysis(record, (str(auto["quote"][0]), str(auto["quote"][1])),
                                     examples=example_blocks(entry))
                if record is not None else None)
        if text is not None and check_entry_analysis(text, entry, project_root, paths)[1]:
            notes.append(KB_AUTO_INVALID_NOTE.format(number=int(file_id), entry_id=auto["id"]))
            text = None
        if text is None:
            path.unlink(missing_ok=True)
            written.pop(file_id, None)
            continue
        if current != text:
            ws.write_text(path, text)
        written[file_id] = _digest(text)
        active.add(file_id)
    if written:
        state[KB_AUTO_STATE] = written
    else:
        state.pop(KB_AUTO_STATE, None)
    return active, notes


def _exact_now(
    run: dict[str, Any],
    entry: dict[str, Any],
    paths: ws.RunPaths,
    entry_id: str,
) -> KBRecord | None:
    """Запись по текущему файлу, если она узнаёт ошибку кластера точно и с тем же логом."""
    kb_dir = str(entry.get("kb_dir") or "")
    if not kb_dir or not Path(kb_dir).is_dir():
        return None
    try:
        record = ProjectKB(Path(kb_dir)).get(entry_id)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if record is None or not _record_steps(record):
        return None
    if entry.get("log_digest") not in record.confirmed_logs:
        return None
    # Сигнатуры всех тестов группы (prepare): отказ по сигнатуре любого из них — после разбора
    # другой проблемы — снимает разбор всей группы (ревью Codex).
    haystack = normalize_fp(_evidence(entry, paths))
    tests = entry_tests(run, entry)
    signatures = (entry.get("kb_auto") or {}).get("signatures") or [entry.get("signature")]
    exact = all(record_matches(record, signature, haystack, tests) == "exact"
                for signature in {entry.get("signature"), *signatures})
    return record if exact else None


def kb_auto_written(state: Mapping[str, Any], file_id: str, text: str) -> bool:
    """Разбор проблемы — тот, что записал скрипт по базе знаний (``sync_known_auto``)."""
    written = state.get(KB_AUTO_STATE)
    return bool(text.strip()) and isinstance(written, dict) and written.get(file_id) == _digest(text)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

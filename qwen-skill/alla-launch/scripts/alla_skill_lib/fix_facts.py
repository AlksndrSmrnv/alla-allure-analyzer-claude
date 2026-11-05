"""Факты проверенных правок: ``<kb_dir>/fixes/*.json``.

Решение пользователя (2026-11-05, план ``wondrous-popping-peacock``): правку автотеста, на
которую инженер сказал «да» и после которой тесты проблемы прошли с первого перезапуска,
скилл запоминает сам, без вопроса. В следующем прогоне та же ошибка тех же тестов (сигнатура,
отпечаток лога, тесты) разбирается скриптом: правка уже в коде — ждать, пока она дойдёт до
CI; правки в коде нет — готовое предложение для ``apply``, без модели.

Это **не** запись базы знаний (``KBRecord``): ``record_matches`` факт не видит, ``confirm()``
не вызывается — точное узнавание по-прежнему только из слов инженера (второе мнение Codex и
Grok: прохождение после правки — факт эксперимента, а не подтверждённая причина). Поэтому
факт действует ограниченно: только та же сигнатура **и** тот же лог, только тесты, на которых
правку проверили, только ``MAX_AGE_DAYS`` дней, и запись базы знаний, узнающая проблему
точно, важнее. В файле — место и строки правки, без текста разбора модели. ``ProjectKB.load``
подпапки не читает, как и ``flaky/``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from collections.abc import Iterator, Mapping
from typing import Any

from alla_core.models.clustering import FailureCluster
from alla_core.models.testops import FailedTestSummary
from alla_skill_lib import flaky, rerun
from alla_skill_lib import workspace as ws
from alla_skill_lib.kb import (
    MAX_RECORD_TESTS,
    README,
    ProjectKB,
    entry_tests,
    kb_label,
    normalize_fp,
    record_matches,
    secret_lines,
)
from alla_skill_lib.sources import Registry, check_entry_analysis, example_blocks
from alla_skill_lib.proposals import (
    Proposal,
    ProposalFiles,
    applied_state,
    location_state,
    parse_proposal,
    proposal_hash,
    timeouts_increased,
)

FIXES_DIRNAME = "fixes"
MAX_AGE_DAYS = 14
FIX_CHANGED = "правка агента применена"  # ``rerun._changed``: тест шёл на коде с правкой агента
# Ключ ``state.json``: file_id → хэши разбора и предложения, которые записал скрипт по факту.
FIX_AUTO_STATE = "fix_auto"
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True)
class FixFact:
    """Одна проверенная правка."""

    id: str
    signature: str
    log_digest: str
    tests: list[str]
    file: str
    line: int
    before: list[str]
    after: list[str]
    launch_id: Any
    date: str
    saved_at: str = ""  # время записи: из фактов одного дня свежее — позже записанный

    def proposal(self) -> Proposal:
        return Proposal("fix", self.file, self.line, list(self.before), list(self.after),
                        why=fix_why(self))

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "signature": self.signature, "log_digest": self.log_digest,
                "tests": self.tests, "file": self.file, "line": self.line,
                "before": self.before, "after": self.after, "launch_id": self.launch_id,
                "date": self.date, "saved_at": self.saved_at}

    @classmethod
    def from_json(cls, data: Any) -> FixFact | None:
        if not isinstance(data, dict):
            return None
        try:
            fact = cls(
                id=str(data["id"]), signature=str(data["signature"]),
                log_digest=str(data["log_digest"]),
                tests=[str(test) for test in data["tests"]],
                file=str(data["file"]), line=int(data["line"]),
                before=[str(line) for line in data["before"]],
                after=[str(line) for line in data["after"]],
                launch_id=data.get("launch_id"), date=str(data["date"]),
                saved_at=str(data.get("saved_at") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not _ID_RE.match(fact.id) or not fact.tests or not fact.before or not fact.after:
            return None
        return fact


def fact_id(signature: str, log_digest: str, file: str, line: int, after: list[str]) -> str:
    """Свой у каждой пары сигнатура + лог (сигнатура, которая сама называет причину, лог не
    берёт, и разные причины за ней — разные факты) и у каждой правки (другая проверенная правка
    той же строки — другой эксперимент, а не перезапись прежнего) — ревью Grok."""
    # JSON, а не склейка через «|»: строка СТАЛО с «|» иначе совпала бы с двумя строками (ревью Grok).
    key = json.dumps([signature, log_digest, file, line, after], ensure_ascii=False)
    return hashlib.sha1(key.encode()).hexdigest()[:12]


class FixStore:
    """Факты в папке ``fixes/`` базы знаний; битый файл пропускается."""

    def __init__(self, kb_dir: Path) -> None:
        self.kb_dir = kb_dir
        self.folder = kb_dir / FIXES_DIRNAME

    def path_for(self, fact: FixFact) -> Path:
        stem = Path(fact.file).stem
        slug = _SLUG_RE.sub("_", stem).strip("_")[:60] or "fix"
        return self.folder / f"{slug}_{fact.line}_{fact.id}.json"

    def load(self) -> list[FixFact]:
        facts: list[FixFact] = []
        if not self.folder.is_dir():
            return facts
        for path in sorted(self.folder.glob("*.json")):
            try:
                fact = FixFact.from_json(json.loads(path.read_text(encoding="utf-8-sig")))
            except (OSError, ValueError):
                continue
            if fact is not None:
                facts.append(fact)
        return facts

    def get(self, wanted: str) -> FixFact | None:
        return next((fact for fact in self.load() if fact.id == wanted), None)

    def save(self, fact: FixFact) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        readme = self.kb_dir / "README.md"
        if not readme.exists():
            ws.write_atomic(readme, README)
        path = self.path_for(fact)
        ws.write_atomic(path, json.dumps(fact.to_json(), ensure_ascii=False, indent=2,
                                         sort_keys=True) + "\n")
        return path

    def forget(self, wanted: str) -> list[Path]:
        removed: list[Path] = []
        for path in sorted(self.folder.glob("*.json")) if self.folder.is_dir() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("id") == wanted:
                path.unlink()
                removed.append(path)
        return removed


# ---------------------------------------------------------------------------
# Запись по итогу перезапуска
# ---------------------------------------------------------------------------


def record_checked(
    paths: ws.RunPaths, run: dict[str, Any], state: rerun.JobState, today: str,
    saved: list[tuple[Path, bytes | None]] | None = None,
) -> tuple[list[str], list[str]]:
    """Записать правки, которые итог перезапуска проверил; строки ответа и папки базы знаний.

    ``saved`` — куда дописывать файлы с их прежним содержимым: при ошибке записи посреди итога
    ``learn`` возвращает прежнее (новый факт удаляет, перезаписанный — восстанавливает), и
    следующий показ проверяет условия заново (ревью Grok, Codex).

    Все условия сразу: правка применена командой ``apply`` и стоит (``applied``), не
    увеличивает таймаут (тест пройдёт и при другой причине), в ней нет строк, похожих на секрет; каждый упавший тест проблемы прошёл с первого запуска на коде с
    правкой, без повторов surefire.
    """
    assert state.result is not None and state.plan is not None
    project_root = Path(run["project_root"])
    keys = flaky.keys_of_tests(run)
    items = {int(item["test_result_id"]): item for item in state.result.get("tests") or []}
    variants = {int(test["test_result_id"]): [int(other) for other in test.get("other_ids") or []]
                for test in state.plan.get("tests") or []}
    sources = {int(test["test_result_id"]): str(test.get("source") or "")
               for test in state.plan.get("tests") or []}
    clean = {int(test["test_result_id"]): bool(test.get("source_clean"))
             for test in state.plan.get("tests") or []}
    planned_fix = {int(test["test_result_id"]): str(test.get("fix_sha") or "")
                   for test in state.plan.get("tests") or []}
    lines: list[str] = []
    folders: set[str] = set()
    for entry in run["clusters"]:
        file_id = entry["file_id"]
        own = [item for item in items.values() if item.get("file_id") == file_id]
        if not own or not entry.get("kb_dir") or not entry.get("signature") \
                or not entry.get("log_digest"):
            continue
        proposal = _applied_proposal(paths, file_id, project_root)
        if proposal is None or not all(_checked(item) for item in own):
            continue
        # Тест прошёл ровно на этой правке: файл правки — как его оставил apply, а другие
        # исходники тестов проблемы не изменены (``changed`` о них молчит, когда есть правка
        # агента — ревью Codex).
        record = _record(paths, file_id)
        if record is None or record["proposal"] != proposal_hash(proposal) \
                or not _as_applied(record, project_root) \
                or any(planned_fix.get(int(item["test_result_id"])) != record["sha_after"]
                       for item in own) \
                or any(rerun.project_path(sources.get(int(item["test_result_id"])) or "")
                       != rerun.project_path(proposal.file or "")
                       and not clean.get(int(item["test_result_id"])) for item in own):
            # Предложение переписали после apply (отметка — о другом), перезапуск шёл на другой
            # версии правки (её откатили и применили другую), до apply
            # в файле уже была ручная правка (её не запомнить, ``CLEAN_BEFORE``), или менялись другие исходники
            # тестов — тест мог пройти не из-за этой правки (ревью Codex).
            continue
        covered = {test_id for item in own
                   for test_id in (int(item["test_result_id"]),
                                   *variants.get(int(item["test_result_id"]), []))}
        tests = [int(test["test_result_id"]) for test in rerun.failed_tests_of(run, entry)]
        names = sorted({keys.get(test_id, "") for test_id in tests})
        if not set(tests) <= covered or "" in names or len(names) > MAX_RECORD_TESTS:
            continue
        assert proposal.file is not None and proposal.line is not None
        file = rerun.project_path(proposal.file)
        fact = FixFact(fact_id(str(entry["signature"]), str(entry["log_digest"]), file,
                               proposal.line, list(proposal.after)),
                       str(entry["signature"]), str(entry["log_digest"]), names, file,
                       proposal.line, list(proposal.before), list(proposal.after),
                       run.get("launch_id"), today, datetime.now().isoformat(timespec="seconds"))
        store = FixStore(Path(entry["kb_dir"]))
        # Чтение, объединение, запись и откат — под блокировкой проекта ``kb_write_lock``,
        # которую держит вызывающий (``learn``): итоги двух разборов иначе затирали бы тесты
        # и откат друг друга (ревью Codex, Grok).
        known = store.get(fact.id)
        if known is not None:
            # Та же правка, проверенная заново на части тестов: прежнее покрытие не теряется
            # (ревью Grok). Новые важнее старых, не больше MAX_RECORD_TESTS.
            merged = list(dict.fromkeys([*names, *known.tests]))[:MAX_RECORD_TESTS]
            fact = replace(fact, tests=sorted(merged))
        target = store.path_for(fact)
        previous = target.read_bytes() if target.is_file() else None
        if saved is not None:
            saved.append((target, previous))  # до записи: упавшая запись тоже откатится
        store.save(fact)
        folders.add(kb_label(project_root, Path(entry["kb_dir"])))
        lines.append(f"Запомнена проверенная правка проблемы {int(file_id)} ({fact.file}:"
                     f"{fact.line}): в следующих прогонах эта же ошибка этих тестов разберётся "
                     "без агента.")
    return lines, sorted(folders)


@contextlib.contextmanager
def kb_write_lock(project_root: Path) -> Iterator[None]:
    """Блокировка записи скрипта в ``flaky/`` и ``fixes/`` на весь проект: итог перезапуска с
    откатом и ``reject N fix:<id>``. Файл — в папке отчётов по умолчанию проекта, рядом с
    блокировкой перезапуска (``rerun.project_lock``), а не в ``--reports-dir``: разборы одного
    проекта с разными папками отчётов пишут одну базу знаний (ревью Codex). Не вкладывается сама
    в себя (``flock`` на втором дескрипторе ждал бы вечно)."""
    import fcntl

    lock = rerun.project_lock(project_root).with_name(".kb-write.lock")
    with open(lock, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _checked(item: dict[str, Any]) -> bool:
    return (item.get("outcome") == "passed" and int(item.get("attempt") or 0) == 1
            and not item.get("surefire_flaky") and item.get("changed") == FIX_CHANGED
            and not item.get("edited"))


CLEAN_BEFORE = "clean_before"  # поле отметки apply: до правки файл был как в HEAD git


def note_clean_before(paths: ws.RunPaths, file_id: str, project_root: Path) -> None:
    """Сразу после ``apply``: был ли файл до правки как в ``HEAD`` git (без ручных правок).

    Проверяется в момент apply, а не при записи факта: коммит самой правки до показа итога
    перезапуска иначе отменял бы запоминание (ревью Grok). Свои поля отметки ``apply`` не
    мешают (``proposals._load_record`` берёт только свои ключи).
    """
    path = paths.proposal_record(file_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        before = paths.proposal_backup(file_id).read_bytes()  # файл до правки (``NN.orig``)
        clean = (hashlib.sha256(before).hexdigest() == str(data["sha_before"])
                 and rerun.git_head_matches(project_root, str(data["file"]), before))
        ws.write_atomic(path, json.dumps({**data, CLEAN_BEFORE: clean}, ensure_ascii=False,
                                         indent=2) + "\n")
    except (OSError, ValueError, KeyError, TypeError):
        return


def _record(paths: ws.RunPaths, file_id: str) -> dict[str, str] | None:
    """Отметка ``apply`` проблемы: файл и хэши до и после."""
    try:
        data = json.loads(paths.proposal_record(file_id).read_text(encoding="utf-8"))
        if data.get(CLEAN_BEFORE) is not True:
            return None  # ручная правка до apply или git не ответил
        return {key: str(data[key]) for key in ("proposal", "file", "sha_before", "sha_after")}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _as_applied(record: dict[str, str], project_root: Path) -> bool:
    """Файл правки сейчас ровно тот, что записал ``apply`` (``sha_after`` отметки)."""
    try:
        target = project_root / record["file"]
        return hashlib.sha256(target.read_bytes()).hexdigest() == record["sha_after"]
    except OSError:
        return False


def _applied_proposal(paths: ws.RunPaths, file_id: str, project_root: Path) -> Proposal | None:
    path = paths.proposal(file_id)
    try:
        proposal = parse_proposal(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    if not proposal.is_fix or not proposal.file or proposal.line is None \
            or not proposal.before or not proposal.after:
        return None
    files = ProposalFiles(paths.proposal_record(file_id), paths.proposal_backup(file_id),
                          paths.proposal_patch(file_id))
    if applied_state(proposal, project_root, files) != "applied":
        return None
    # Рост таймаута лечит симптом: тест пройдёт и при другой причине. Смену ожидаемого значения
    # инженер видел строкой «Проверь:» и согласился — её проверенный итог запоминается.
    if timeouts_increased(proposal.before, proposal.after):
        return None
    if secret_lines("\n".join([*proposal.before, *proposal.after, proposal.file])):
        return None
    return proposal


# ---------------------------------------------------------------------------
# Узнавание в следующем прогоне
# ---------------------------------------------------------------------------


def match(entry: dict[str, Any], tests: list[str], project_root: Path,
          today: date | None = None) -> dict[str, Any] | None:
    """Факт, который разбирает кластер, — ``{"id", "state", "file", "line", "date",
    "launch_id"}`` для ``entry["fix_auto"]``; иначе ``None``.

    Сигнатура и отпечаток лога те же, все тесты кластера — из проверенных, факт не старше
    ``MAX_AGE_DAYS``, запись базы знаний не узнаёт кластер точно (слова инженера важнее), и
    место правки в коде однозначно: ``applied`` — правка уже стоит, ``pending`` — её нет.
    Подходят несколько — берётся самый свежий.
    """
    kb_dir = str(entry.get("kb_dir") or "")
    if not kb_dir or not entry.get("signature") or not tests:
        return None
    if any(item.get("origin") == "exact" for item in entry.get("kb") or []):
        return None
    now = today or date.today()
    found = []
    for fact in FixStore(Path(kb_dir)).load():
        if not _fits(fact, entry, tests, now):
            continue
        where = location_state(fact.proposal(), project_root)
        if where in ("applied", "pending"):
            found.append(((fact.date, fact.saved_at), fact, where))
    if not found:
        return None
    _, fact, where = max(found, key=lambda item: item[0])
    return {"id": fact.id, "state": where, "file": fact.file, "line": fact.line,
            "date": fact.date, "launch_id": fact.launch_id}


def _fits(fact: FixFact, entry: dict[str, Any], tests: list[str], today: date) -> bool:
    """Сигнатура и отпечаток лога те же, тесты — из проверенных, факт не старше срока."""
    return (fact.signature == entry.get("signature") and fact.log_digest == entry.get("log_digest")
            and bool(tests) and set(tests) <= set(fact.tests)
            and _age(fact, today) <= MAX_AGE_DAYS)


def _age(fact: FixFact, today: date) -> int:
    try:
        return (today - date.fromisoformat(fact.date)).days
    except ValueError:
        return MAX_AGE_DAYS + 1


def current_fact(entry: dict[str, Any]) -> FixFact | None:
    """Факт ``entry["fix_auto"]`` по текущей папке ``fixes/``; удалён — ``None``."""
    auto = entry.get("fix_auto") or {}
    kb_dir = str(entry.get("kb_dir") or "")
    if not auto or not kb_dir:
        return None
    return FixStore(Path(kb_dir)).get(str(auto.get("id")))


# ---------------------------------------------------------------------------
# Тексты разбора и предложения
# ---------------------------------------------------------------------------


def fix_why(fact: FixFact) -> str:
    # Без даты: повторная проверка не должна переписывать уже применённое предложение (отметка
    # apply хранит его хэш).
    return ("эта правка уже исправляла ту же ошибку этих тестов: после неё они прошли с первого "
            "перезапуска")


def proposal_text(fact: FixFact) -> str:
    """``proposals/NN.md`` из факта: обычное предложение, его показывает и применяет ``apply``."""
    return "\n".join([
        "РЕШЕНИЕ: исправить",
        f"ФАЙЛ: {fact.file}:{fact.line}",
        "БЫЛО:",
        *fact.before,
        "СТАЛО:",
        *fact.after,
        f"ПОЧЕМУ: {fix_why(fact)}.",
    ]) + "\n"


def analysis_text(auto: dict[str, Any], quote: tuple[str, str], *, examples: int = 1,
                  log_not_found: bool = False, kb_offered: bool = False) -> str:
    """Разбор проблемы по факту — в обычном формате, его проверяет ``next``."""
    where = f"{auto['file']}:{auto['line']}"
    # Текст не зависит ни от того, стоит ли правка в коде, ни от даты проверки: иначе apply или
    # повторная проверка меняли бы данные сводки, и пересборка отчёта после перезапуска
    # останавливалась на шаге summary (ревью Codex). Стоит ли правка — показывает предложение.
    # Первое предложение — в кратком разборе: коротко, с id для ``reject N fix:<id>``.
    what = (f"Эту ошибку уже исправляла проверенная правка fix:{auto['id']} "
            f"({Path(str(auto['file'])).name}:{auto['line']}): после неё эти тесты прошли с "
            f"первого перезапуска. Правка — {where}.")
    steps = [f"Правки {where} в коде нет — применить её (она предложена); уже есть — дождаться, "
             "когда она попадёт в ветку, на которой идёт CI, и перезапустить прогон.",
             "Если с этой правкой в коде тест падает так же — она не помогла: скажите об этом, "
             "и проблему разберёт агент."]
    source_id, line = quote
    return "\n".join([
        f"ЧТО СЛОМАЛОСЬ: {what}",
        "",
        f"ПРИЧИНА: тест — дефект кода автотеста: после правки {where} эти же тесты с этой же "
        "ошибкой прошли с первого перезапуска.",
        "",
        "НАБЛЮДЕНИЯ:",
        f"- [{source_id}] «{line}»",
        "",
        "НЕ ХВАТАЕТ: " + ("лога операции этого теста — в общем логе сервиса нет её записей; "
                          "причину показала проверенная правка" if log_not_found else "нет"),
        "",
        *(["СОГЛАСОВАННОСТЬ: одна причина — все тесты группы прошли после этой правки", ""]
          if examples > 1 else []),
        "КАК ИСПРАВИТЬ:",
        *(f"{number}. {step}" for number, step in enumerate(steps, start=1)),
        "",
        f"КОД: {where} — проверенная правка",
        *(["", "БАЗА ЗНАНИЙ: нет"] if kb_offered else []),
        "",
        "ПРИМЕЧАНИЕ: это запись отчёта Аллы в alla-reports, а не правка кода — файлы проекта не "
        "меняются.",
    ]) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# prepare и next
# ---------------------------------------------------------------------------


def prepared(entry: dict[str, Any], cluster: FailureCluster,
             tests_by_id: dict[int, FailedTestSummary], sources: Registry, project_root: Path,
             paths: ws.RunPaths) -> dict[str, Any] | None:
    """``entry["fix_auto"]`` для ``prepare``: факт подходит и его разбор проходит проверку."""
    from alla_skill_lib.kb import cluster_tests
    from alla_skill_lib.known_issues import first_quote  # known_issues тянет весь отчёт

    auto = match(entry, cluster_tests(cluster, tests_by_id), project_root)
    if auto is None or not _members_same(cluster, tests_by_id, str(entry["signature"])):
        return None
    quote = first_quote(sources)
    if quote is None:
        return None
    auto["quote"] = list(quote)
    text = _text(entry, auto)
    if check_entry_analysis(text, entry, project_root, paths)[1]:
        return None
    return auto


def _members_same(cluster: FailureCluster, tests_by_id: dict[int, FailedTestSummary],
                  signature: str) -> bool:
    """Каждый тест группы — та же ошибка, что у представителя: своя сигнатура та же, строки-ошибки
    своего лога (если он есть) — те же, что у лога кластера. Сигнатура и отпечаток лога кластера
    — представителя: другая ошибка другого участника иначе получила бы чужую правку (ревью
    Codex; то же правило, что у ``known_issues.auto_resolution``)."""
    from alla_skill_lib.kb import key_of_test
    from alla_skill_lib.signature import (
        as_representatives,
        cluster_log_errors,
        cluster_signature,
        log_error_lines,
    )

    shown = cluster_log_errors(cluster, tests_by_id)
    for alone in as_representatives(cluster, tests_by_id):
        test = (tests_by_id.get(alone.representative_test_id)
                if alone.representative_test_id is not None else None)
        # Без ключа тест не сверить со списком проверенных (``cluster_tests`` его просто
        # пропустил бы — ревью Codex).
        if test is None or not key_of_test(test.full_name, test.test_case_id) \
                or cluster_signature(alone, tests_by_id) != signature:
            return False
        own = log_error_lines(test)
        if own and own != shown:
            return False
    return True


def _text(entry: dict[str, Any], auto: dict[str, Any]) -> str:
    return analysis_text(auto, (str(auto["quote"][0]), str(auto["quote"][1])),
                         examples=example_blocks(entry),
                         log_not_found=bool(entry.get("log_not_found")),
                         kb_offered=bool(entry.get("kb")))


def current_text(paths: ws.RunPaths, run: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """Текст разбора по факту сейчас, без изменения файлов; не действует — ``None``.

    Одно правило для ``next`` (``sync``) и чтения состояния (``problem_state``, снимок
    расширений): факт есть, место правки в коде однозначно (совет — по текущему файлу), и
    слова инженера не появились — запись базы знаний не узнаёт проблему точно, вывод
    расширения не подтверждён (ревью Codex: иначе отчёт показывал бы и подтверждённую причину
    инженера, и старое предложение правки).
    """
    auto = entry.get("fix_auto")
    fact = current_fact(entry) if auto else None
    # Тот же id мог перезаписать факт другого прогона (другие лог или тесты), факт мог устареть:
    # условия ``match`` — заново (ревью Codex).
    if not auto or fact is None or not _fits(fact, entry, entry_tests(run, entry), date.today()):
        return None
    where = location_state(fact.proposal(), Path(run["project_root"]))
    if where not in ("applied", "pending") or _engineer_said(paths, run, entry):
        return None
    text = _text(entry, {**auto, "file": fact.file, "line": fact.line})
    # Код с тех пор менялся (строка КОД за концом файла): текст, не прошедший проверку, снимается,
    # как у разбора по базе знаний, — иначе next просил бы модель править чужой файл (ревью Grok).
    if check_entry_analysis(text, entry, Path(run["project_root"]), paths)[1]:
        return None
    return text


def _engineer_said(paths: ws.RunPaths, run: dict[str, Any], entry: dict[str, Any]) -> bool:
    if paths.confirmed(entry["file_id"]).is_file():
        return True
    try:
        records, _warnings = ProjectKB(Path(str(entry["kb_dir"]))).load()
    except OSError:
        return False
    evidence = paths.evidence(entry["file_id"])
    haystack = normalize_fp(ws.read_text(evidence) if evidence.is_file() else "")
    tests = entry_tests(run, entry)
    return any(record_matches(record, str(entry.get("signature") or ""), haystack, tests)
               == "exact" for record in records)


SCRIPT_START = "ЧТО СЛОМАЛОСЬ: Эту ошибку уже исправляла проверенная правка fix:"


def fix_auto_written(state: Mapping[str, Any], file_id: str, text: str,
                     entry: Mapping[str, Any] | None = None) -> bool:
    """Разбор проблемы — тот, что записал скрипт по факту (``sync``).

    По хэшу в ``state.json``, а с ``entry`` — ещё и по началу текста с id факта кластера:
    ``state.json`` мог не записаться после разбора, и шаблон скрипта стал бы «чужим» разбором,
    который можно подтвердить как слова инженера (ревью Grok).
    """
    if not text.strip():
        return False
    ours = (state.get(FIX_AUTO_STATE) or {}).get(file_id)
    if isinstance(ours, dict) and ours.get("analysis") == digest(text):
        return True
    auto = (entry or {}).get("fix_auto") or {}
    return bool(auto) and text.startswith(f"{SCRIPT_START}{auto.get('id')} ")


def sync(paths: ws.RunPaths, run: dict[str, Any], state: dict[str, Any]) -> set[str]:
    """Разборы и предложения по фактам проверенных правок: записать или снять (``next``).

    Факт есть — разбор и ``proposals/NN.md`` пишет скрипт (их хэши — в ``state.json``). Факт
    удалили (``reject N fix:<id>``, руками) — свои файлы удаляются, и проблему разбирает модель.
    Файл с другим хэшем — разбор или предложение модели — не трогается; применённое
    предложение (есть отметка ``apply``) тоже. Возвращает номера разобранных так проблем;
    ``state`` меняется на месте.
    """
    written: dict[str, dict[str, str]] = {
        key: dict(value) for key, value in (state.get(FIX_AUTO_STATE) or {}).items()
        if isinstance(value, dict)}
    active: set[str] = set()
    for entry in run["clusters"]:
        auto = entry.get("fix_auto")
        if not auto:
            continue
        file_id = entry["file_id"]
        ours = written.get(file_id, {})
        analysis_path, proposal_path = paths.analysis(file_id), paths.proposal(file_id)
        current = ws.read_text(analysis_path) if analysis_path.is_file() else ""
        if current.strip() and not fix_auto_written(state, file_id, current, entry):
            continue  # разбор после отказа от факта пишет модель
        proposal = ws.read_text(proposal_path) if proposal_path.is_file() else ""
        fact = current_fact(entry)
        own_proposal = (not proposal.strip() or digest(proposal) == ours.get("proposal")
                        or (fact is not None and proposal == proposal_text(fact)))
        # Место правки — по текущему файлу: после apply или отката совет меняется (ревью Codex).
        text = current_text(paths, run, entry)
        if fact is None or text is None:
            if current.strip():
                analysis_path.unlink(missing_ok=True)
            if proposal.strip() and own_proposal and not paths.proposal_record(file_id).is_file():
                proposal_path.unlink(missing_ok=True)
            written.pop(file_id, None)
            continue
        ours = {"analysis": digest(text),
                **({"proposal": written[file_id]["proposal"]}
                   if "proposal" in written.get(file_id, {}) else {})}
        try:
            # Атомарно и с отметкой владельца сразу: сбой записи следующей проблемы не должен
            # оставить шаблон скрипта «чужим» разбором (ревью Grok).
            if current != text:
                analysis_path.parent.mkdir(parents=True, exist_ok=True)
                ws.write_atomic(analysis_path, text)
        except OSError:
            continue  # разбора нет — проблему разберёт модель; следующий next попробует снова
        written[file_id] = dict(ours)
        active.add(file_id)  # разбор — скрипта, даже если предложение ниже не записалось
        if own_proposal and not paths.proposal_record(file_id).is_file():
            wanted = proposal_text(fact)
            try:
                if proposal != wanted:
                    proposal_path.parent.mkdir(parents=True, exist_ok=True)
                    ws.write_atomic(proposal_path, wanted)
                written[file_id]["proposal"] = digest(wanted)
            except OSError:
                continue  # предложение — следующим next; модели его не отдаём (cli)
    if written:
        state[FIX_AUTO_STATE] = written
    else:
        state.pop(FIX_AUTO_STATE, None)
    return active

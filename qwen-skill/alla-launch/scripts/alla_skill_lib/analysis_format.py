"""Разбор и лёгкая проверка анализа кластера, который написал агент.

Формат тот же, что промпт ядра (``build_cluster_analysis_prompt``) требует от модели::

    ЧТО СЛОМАЛОСЬ: ...
    ПРИЧИНА: <тест|приложение|окружение|данные|неизвестно> — ...
    КАК ИСПРАВИТЬ:
    1. ...
    КОД: path/to/Test.java:42 — ...   (необязательно)
    БАЗА ЗНАНИЙ: <id записи> | нет     (если задание предлагало записи)

Тот же парсер читает обратную связь пользователя (``feedback/NN.md``),
где ещё бывают ``НАЗВАНИЕ:`` и ``ПРИЗНАК:``.

Парсер прощает markdown-оформление (``**ПРИЧИНА:**``, ``### Что сломалось``)
и регистр. Проверяется только наличие разделов, категория и существование
файлов из строки ``КОД`` — дешёвая защита от выдуманных ссылок.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePath

from alla_skill_lib.code_hints import SOURCE_EXTENSIONS, ProjectIndex

CATEGORIES = ("тест", "приложение", "окружение", "данные", "неизвестно")
# Формат, который агент видит в ответах ``fix`` и в заданиях субагентов пакетного разбора.
EXPECTED_FORMAT = """\
ЧТО СЛОМАЛОСЬ: <1–2 предложения>
ПРИЧИНА: <тест|приложение|окружение|данные|неизвестно> — <обоснование>
КАК ИСПРАВИТЬ:
1. <шаг>
КОД: <путь от корня проекта>:<строка> — <что там>   (необязательно)
БАЗА ЗНАНИЙ: <id записи> | нет   (только если задание предлагало записи)"""
# Только слова, которые однозначно называют категорию. «сервис», «app», «test»
# и подобные не берём: «Сервис авторизации недоступен (окружение)» — это не
# приложение, а молчаливая ошибка в итогах, истории и базе знаний.
_CATEGORY_ALIASES = {
    "тестовые данные": "данные",
    "не определено": "неизвестно",
    "автотест": "тест",
    "тест": "тест",
    "приложение": "приложение",
    "окружение": "окружение",
    "инфраструктура": "окружение",
    "стенд": "окружение",
    "данные": "данные",
    "неизвестно": "неизвестно",
}
# «приложение или окружение», «тест/данные» — модель не выбрала категорию.
_AMBIGUOUS_AFTER_CATEGORY_RE = re.compile(r"^\s*(?:[/\\|]|или\b)")
_SECTIONS = {
    "что сломалось": "what",
    "причина": "cause",
    "как исправить": "fix",
    "код": "code",
    "база знаний": "kb",
    "название": "title",
    "признак": "fingerprint",
}
SECTION_TITLES = {
    "what": "ЧТО СЛОМАЛОСЬ",
    "cause": "ПРИЧИНА",
    "fix": "КАК ИСПРАВИТЬ",
}
_HEADER_NAMES = "|".join(sorted(_SECTIONS, key=len, reverse=True))
# Оформление и нумерация перед именем раздела («### », «- **», «1. **»), затем
# «:» или тире с пробелами. «Код-ревью» и «Код ответа: 504» заголовком не являются.
_HEADER_RE = re.compile(
    rf"^[\s#>*_`\-•]*(?:\d+[.)]\s*)?[\s#>*_`]*(?P<name>{_HEADER_NAMES})[*_`]*\s*"
    r"(?:[:：][*_`]*\s*(?P<colon>.*)|[—–-]+(?=\s|$)\s*(?P<dash>.*)|$)",
    re.IGNORECASE,
)
_LIST_MARKER_RE = re.compile(r"(?:^|(?<=\s))(?:[-*•]|\d+[.)])(?=\s)")
# Строка, похожая на заголовок раздела (ЗАГЛАВНЫМИ), но с неизвестным названием.
_UNKNOWN_HEADER_RE = re.compile(r"^\W*\d*\W*([А-ЯЁ][А-ЯЁ ]{3,40}?)\s*[:：]")
_ID_TOKEN_RE = re.compile(r"^[a-z0-9_]{1,100}$")
_NO_KB = {"нет", "-", "—", "none", "no"}
_BOLD_RE = re.compile(r"\*\*")
_PATH_RE = re.compile(r"(?P<path>[\w.\-/\\]+\.[A-Za-z0-9]{1,8})(?::(?P<line>\d+))?")
_CONFIG_EXTENSIONS = frozenset({
    ".yaml", ".yml", ".json", ".xml", ".properties", ".conf", ".cfg", ".ini",
    ".toml", ".gradle", ".sql", ".md", ".txt", ".env", ".csv",
})
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")
_STEP_PREFIX_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
MAX_CHECKED_FILE_BYTES = 5_000_000
# Потолки полей сжатого разбора: сводка по прогону не должна раздувать контекст модели.
COMPACT_CAUSE_CHARS = 240
COMPACT_WHAT_CHARS = 160
COMPACT_STEP_CHARS = 160


@dataclass
class ClusterAnalysis:
    """Разобранный анализ кластера."""

    raw: str
    what: str = ""
    cause: str = ""
    category: str | None = None
    fix: str = ""
    code: list[str] = field(default_factory=list)
    kb_ref: str | None = None
    title: str = ""
    fingerprint: str = ""
    # Строки вида «ЧТО ПОШЛО НЕ ТАК:», похожие на заголовок, но не из формата.
    unrecognized: list[str] = field(default_factory=list)

    @property
    def cause_reason(self) -> str:
        """Текст ПРИЧИНЫ без категории в начале."""
        text = _strip_category(self.cause)
        return text or self.cause

    def compact(self) -> str:
        """Сжатый разбор для задания на общий анализ.

        Причина с категорией, первое предложение «что сломалось» и первый шаг
        исправления — из них сводка собирает ключевые проблемы и приоритетные
        исправления, не тратя контекст на полные разборы. Каждое поле
        обрезано: длинная причина одного кластера не должна вытеснять остальные.
        """
        lines = [f"ПРИЧИНА: {_clip(_one_line(self.cause), COMPACT_CAUSE_CHARS)}"]
        what = _clip(self.what_first_sentence(), COMPACT_WHAT_CHARS)
        if what:
            lines.append(f"ЧТО СЛОМАЛОСЬ: {what}")
        step = _clip(self.first_fix_step(), COMPACT_STEP_CHARS)
        if step:
            lines.append(f"ПЕРВЫЙ ШАГ ИСПРАВЛЕНИЯ: {step}")
        return "\n".join(lines)

    def what_first_sentence(self) -> str:
        """Первое предложение «ЧТО СЛОМАЛОСЬ» в одну строку."""
        return _SENTENCE_END_RE.split(_one_line(self.what), maxsplit=1)[0]

    def first_fix_step(self) -> str:
        for line in self.fix.splitlines():
            step = _STEP_PREFIX_RE.sub("", line).strip()
            if step:
                return step
        return ""


def parse_analysis(text: str) -> ClusterAnalysis:
    """Разобрать текст анализа на разделы (неизвестный текст до разделов игнорируется).

    Заголовок раздела принимается один раз (кроме ``КОД``, который бывает в
    нескольких строках). Пункт списка с обычным написанием — ``- Код: …`` или
    ``2. Причина: …`` — внутри шагов исправления остаётся текстом шага.
    """
    analysis = ClusterAnalysis(raw=text.strip())
    buckets: dict[str, list[str]] = {key: [] for key in _SECTIONS.values()}
    seen: set[str] = set()
    current: str | None = None
    for raw_line in text.lstrip("\ufeff").splitlines():
        header = _match_header(raw_line, current, seen)
        if header is not None:
            current, rest = header
            seen.add(current)
            if rest:
                buckets[current].append(rest)
            continue
        if current is not None:
            buckets[current].append(raw_line.rstrip())
        if (
            len(analysis.unrecognized) < 3
            and _HEADER_RE.match(raw_line) is None
            and _UNKNOWN_HEADER_RE.match(raw_line)
        ):
            analysis.unrecognized.append(raw_line.strip()[:80])

    analysis.what = _join(buckets["what"])
    analysis.cause = _join(buckets["cause"])
    analysis.fix = _join(buckets["fix"])
    analysis.code = [line.strip(" -*") for line in buckets["code"] if line.strip(" -*")]
    analysis.category = detect_category(analysis.cause)
    analysis.kb_ref = _kb_ref(_join(buckets["kb"]))
    analysis.title = _one_line(_join(buckets["title"]))
    analysis.fingerprint = _join(buckets["fingerprint"])
    return analysis


def _match_header(
    raw_line: str,
    current: str | None,
    seen: set[str],
) -> tuple[str, str] | None:
    """(раздел, остаток строки) или None, если строка не заголовок раздела."""
    match = _HEADER_RE.match(raw_line)
    if match is None:
        return None
    key = _SECTIONS[match.group("name").lower()]
    if key in seen and key != "code":
        return None
    if current == "fix" and _is_plain_list_item(raw_line, match):
        return None
    rest = match.group("colon") if match.group("colon") is not None else match.group("dash")
    return key, _BOLD_RE.sub("", rest or "").strip()


def _is_plain_list_item(raw_line: str, match: re.Match[str]) -> bool:
    """«- Код: …» или «2. Причина: …» без жирного и ЗАГЛАВНЫХ — шаг, а не раздел."""
    prefix = raw_line[: match.start("name")]
    if not _LIST_MARKER_RE.search(prefix):
        return False
    written = raw_line[match.start("name"): match.end("name")]
    emphasised = bool(re.search(r"[*_#]", _LIST_MARKER_RE.sub("", prefix, count=1))) or bool(
        re.match(r"[*_]", raw_line[match.end("name"):])
    )
    return not (written.isupper() or emphasised)


def parse_summary(analysis: ClusterAnalysis) -> str:
    """Что парсер понял в разборе: подсказка модели, когда формат не принят."""
    parts = []
    for key, title in SECTION_TITLES.items():
        value = getattr(analysis, key)
        part = f"{title} {'✓' if value else '✗'}"
        if key == "cause" and value:
            part += f" (категория: {analysis.category or 'не распознана'})"
        parts.append(part)
    line = "Разобрано: " + ", ".join(parts)
    if analysis.unrecognized:
        line += (
            "\nСтроки, похожие на разделы, но не из формата (названия разделов "
            "менять нельзя): " + "; ".join(analysis.unrecognized)
        )
    return line


def _kb_ref(value: str) -> str | None:
    """id записи из строки «БАЗА ЗНАНИЙ:»; «нет», «-» и любая фраза — None."""
    words = value.strip().strip("`[]«»\"'()").split()
    if not words or words[0].lower().strip(".,") in _NO_KB:
        return None
    first = words[0].strip("`[]«»\"'().,;:").lower()
    if not _ID_TOKEN_RE.match(first):
        return None  # «подходящих записей нет», «не применимо»
    if len(words) > 1 and words[1][:1].isalnum() and not re.search(r"[_\d]", first):
        return None  # «not applicable»: у настоящих id есть «_» и хэш
    return first


def detect_category(cause: str) -> str | None:
    """Категория из начала раздела ПРИЧИНА: ``приложение — ...``, ``[тест] ...``.

    ``None``, если первое слово не категория или названо сразу две
    («приложение или окружение», «тест/данные»).
    """
    head = cause.strip().lstrip("[«\"'(*`_").lower()
    for alias in sorted(_CATEGORY_ALIASES, key=len, reverse=True):
        if head.startswith(alias):
            following = head[len(alias):len(alias) + 1]
            if following and following.isalnum():
                continue
            if _AMBIGUOUS_AFTER_CATEGORY_RE.match(head[len(alias):].lstrip("]»\"')*`_")):
                return None
            return _CATEGORY_ALIASES[alias]
    return None


def validate_analysis(
    analysis: ClusterAnalysis,
    project_root: Path,
    offered_kb: frozenset[str] = frozenset(),
) -> list[str]:
    """Список проблем формата (пусто — анализ принят)."""
    errors: list[str] = []
    if not analysis.what:
        errors.append("нет раздела «ЧТО СЛОМАЛОСЬ:»")
    if not analysis.cause:
        errors.append("нет раздела «ПРИЧИНА:»")
    elif analysis.category is None:
        errors.append(
            "в «ПРИЧИНА:» первым словом должна идти одна категория: "
            + " / ".join(CATEGORIES)
            + " (без «или» и «/»; например «ПРИЧИНА: окружение — стенд недоступен»)"
        )
    if not analysis.fix:
        errors.append("нет раздела «КАК ИСПРАВИТЬ:» с шагами исправления")
    errors.extend(code_ref_errors(analysis, project_root))
    if analysis.kb_ref and analysis.kb_ref not in offered_kb:
        offered = ", ".join(sorted(offered_kb)) or "в задании записей не было"
        errors.append(
            f"в «БАЗА ЗНАНИЙ:» запись «{analysis.kb_ref}» не предлагалась для этого "
            f"кластера ({offered}) — укажи id из задания или «нет»"
        )
    return errors


def code_ref_errors(analysis: ClusterAnalysis, project_root: Path) -> list[str]:
    """Проблемы ссылок из раздела КОД: нет файла в проекте или строка вне файла."""
    errors: list[str] = []
    root = project_root.resolve()
    for line in analysis.code:
        match = _PATH_RE.search(line)
        if not match:
            continue
        raw = match.group("path").replace("\\", "/")
        suffix = PurePath(raw).suffix.lower()
        if "/" not in raw and suffix not in SOURCE_EXTENSIONS | _CONFIG_EXTENSIONS:
            continue  # похоже на вызов метода (orderApi.create), а не на файл
        candidate = Path(raw)
        path = candidate if candidate.is_absolute() else root / candidate
        try:
            resolved = path.resolve()
            inside = resolved == root or root in resolved.parents
        except OSError:
            inside = False
        if inside and not resolved.is_file() and "/" not in raw and suffix in SOURCE_EXTENSIONS:
            # «OrderTest.java:6» из кадра стека: без пути принимаем однозначное имя файла.
            found = [c for c in _index(root).candidates(PurePath(raw).stem) if c.name == raw]
            if len(found) > 1:
                options = ", ".join(sorted(c.as_posix() for c in found)[:3])
                errors.append(
                    f"в «КОД:» имя «{raw}» встречается в нескольких файлах ({options}) — "
                    "укажи путь относительно корня проекта"
                )
                continue
            if found:
                resolved = (root / found[0]).resolve()
        if not inside or not resolved.is_file():
            errors.append(
                f"в «КОД:» файл «{raw}» не найден в проекте {root} — "
                "укажи путь относительно корня проекта или удали строку КОД"
            )
            continue
        number = match.group("line")
        total = _line_count(resolved) if number else None
        if number and total is not None and not 1 <= int(number) <= total:
            errors.append(
                f"в «КОД:» строка {number} вне файла «{raw}» (в файле {total} строк) — "
                "укажи строку, которая есть в файле"
            )
    return errors


_index_cache: dict[Path, ProjectIndex] = {}


def _index(root: Path) -> ProjectIndex:
    """Индекс исходников проекта; `next` проверяет все разборы подряд, обход диска один на процесс."""
    if root not in _index_cache:
        _index_cache[root] = ProjectIndex(root)
    return _index_cache[root]


def _line_count(path: Path) -> int | None:
    try:
        if path.stat().st_size > MAX_CHECKED_FILE_BYTES:
            return None
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return None


def _strip_category(cause: str) -> str:
    category = detect_category(cause)
    if category is None:
        return cause.strip()
    head = cause.strip().lstrip("[«\"'(*`_")
    for alias in sorted(_CATEGORY_ALIASES, key=len, reverse=True):
        if head.lower().startswith(alias):
            head = head[len(alias):]
            break
    return head.lstrip(" ]»\"')*`_—–-:").strip()


def _join(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

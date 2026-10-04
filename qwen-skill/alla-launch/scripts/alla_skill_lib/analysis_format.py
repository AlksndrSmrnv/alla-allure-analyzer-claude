"""Разбор и проверка анализа кластера, который написал агент.

Формат задания версии 2 (``task_format: 2`` в записи кластера ``run.json``)::

    ЧТО СЛОМАЛОСЬ: ...
    ПРИЧИНА: <тест|приложение|окружение|данные|неизвестно> — ...
    НАБЛЮДЕНИЯ:
    - [S3] «дословная цитата из этого источника»
    НЕ ХВАТАЕТ: ... | нет
    КАК ИСПРАВИТЬ:
    1. ...
    КОД: path/to/Test.java:42 — ...   (необязательно)
    БАЗА ЗНАНИЙ: <id записи> | нет     (если задание предлагало записи)

Цитаты наблюдений проверяются по реестру источников (``sources.py``). Разборы
старых папок (записи без ``task_format``) проверяются по прежним правилам:
без наблюдений и «НЕ ХВАТАЕТ».

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
from typing import Any

from alla_skill_lib.code_hints import SOURCE_EXTENSIONS, ProjectIndex

CATEGORIES = ("тест", "приложение", "окружение", "данные", "неизвестно")
# Формат, который агент видит в ответах ``fix`` и в заданиях субагентов пакетного разбора.
TASK_FORMAT = 2  # формат задания и разбора новых папок (записи кластера в run.json)
EXPECTED_FORMAT = """\
ЧТО СЛОМАЛОСЬ: <что увидел тест, 1–2 предложения>
ПРИЧИНА: <тест|приложение|окружение|данные|неизвестно> — <предполагаемая причина>
НАБЛЮДЕНИЯ:
- [S<номер>] «<дословная цитата из этого куска данных>»
НЕ ХВАТАЕТ: <каких данных нет и какая проверка различит версии> | нет
СОГЛАСОВАННОСТЬ: одна причина | разные проблемы — <чем отличаются примеры> | недостаточно данных   (только если в данных несколько примеров)
КАК ИСПРАВИТЬ:
1. <шаг>
КОД: <путь от корня проекта>:<строка> — <что там>   (необязательно)
БАЗА ЗНАНИЙ: <id записи> | нет   (только если задание предлагало записи)"""
# Разборы папок, созданных до наблюдений (запись кластера без ``task_format``).
LEGACY_EXPECTED_FORMAT = """\
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
    "наблюдения": "observations",
    "не хватает": "missing",
    "согласованность": "consistency",
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
_OBSERVATION_RE = re.compile(
    r"^\s*(?:[-*•]|\d+[.)])?\s*[*_]*\[?\s*(?P<id>S\d+)\s*\]?[*_]*\s*[:—–-]?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
# После закрывающей кавычки цитаты — конец строки или пояснение: « — …», « (…)».
_AFTER_QUOTE_RE = re.compile(r"\s*$|\s+[—–(-]")
# Открывающая кавычка → закрывающая. Парные (« », “ ”) вкладываются друг в друга;
# одинаковые с двух сторон (" ' `) внутри цитаты встречаются чётное число раз.
_QUOTE_PAIRS = {"«": "»", "“": "”", "„": "“", "‘": "’", '"': '"', "'": "'", "`": "`"}
_OPEN_QUOTES = "".join(_QUOTE_PAIRS)
_CONSISTENCY_KINDS = (
    ("одна причина", "same"),
    ("разные проблемы", "different"),
    ("недостаточно данных", "insufficient"),
)
_NOTHING_MISSING = {"нет", "-", "—", "ничего", "всего хватает"}
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
_QUOTED_LOCATION_RE = re.compile(
    r"(?P<quote>[`\"'])(?P<content>.+?)(?P=quote)(?::(?P<line>\d+))?"
)
_LOCATION_RE = re.compile(r"(?P<path>.+?\.[A-Za-z0-9]{1,8})(?::(?P<line>\d+))?")
_LOCATION_END_RE = re.compile(r"\.[A-Za-z0-9]{1,8}(?::\d+)?(?=$|\s)")
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
COMPACT_MISSING_CHARS = 160
UNCONFIRMED_NOTE = "причина не подтверждена логом"


@dataclass(frozen=True)
class Observation:
    """Наблюдение разбора: id куска данных и дословная цитата из него."""

    source_id: str
    quote: str


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
    observations: list[Observation] = field(default_factory=list)
    # Строки раздела НАБЛЮДЕНИЯ не в формате «- [S3] «цитата»».
    bad_observations: list[str] = field(default_factory=list)
    missing: str = ""
    consistency: str = ""
    # Строки вида «ЧТО ПОШЛО НЕ ТАК:», похожие на заголовок, но не из формата.
    unrecognized: list[str] = field(default_factory=list)
    # Реестр источников кластера (id → откуда кусок данных); заполняет проверка
    # разбора нового формата, нужен отчёту для подписи цитат.
    sources: dict[str, dict[str, Any]] | None = None

    @property
    def unconfirmed_by_log(self) -> bool:
        """Причина вне теста опирается только на сообщение и трейс теста, без лога.

        Для категорий «приложение», «окружение», «данные» разбора нового формата
        (есть реестр источников): ни одна цитата не из лога.
        """
        if self.sources is None or self.category in (None, "тест", "неизвестно"):
            return False
        kinds = {str(self.sources.get(item.source_id, {}).get("kind")) for item in self.observations}
        return bool(kinds) and kinds <= {"message", "trace"}

    @property
    def consistency_kind(self) -> str | None:
        """``same`` | ``different`` | ``insufficient`` из «СОГЛАСОВАННОСТЬ»; иначе ``None``."""
        head = _one_line(self.consistency).lower().lstrip("«\"'*_ ")
        for prefix, kind in _CONSISTENCY_KINDS:
            if head.startswith(prefix):
                return kind
        return None

    @property
    def consistency_detail(self) -> str:
        """Чем отличаются примеры: текст после «разные проблемы —»."""
        if self.consistency_kind != "different":
            return ""
        text = _one_line(self.consistency)
        return text[len("разные проблемы"):].lstrip(" —–-:,.").strip()

    @property
    def missing_text(self) -> str:
        """«НЕ ХВАТАЕТ» по существу; «нет» и пусто — пустая строка."""
        text = _one_line(self.missing)
        return "" if text.lower().strip(" .") in _NOTHING_MISSING else text

    @property
    def cause_reason(self) -> str:
        """Текст ПРИЧИНЫ без категории в начале."""
        text = _strip_category(self.cause)
        return text or self.cause

    def compact(self) -> str:
        """Сжатый разбор для задания на общий анализ.

        Причина с категорией, первое предложение «что сломалось», чего не хватает и
        первый шаг исправления — из них сводка собирает ключевые проблемы и приоритетные
        исправления, не тратя контекст на полные разборы. Каждое поле
        обрезано: длинная причина одного кластера не должна вытеснять остальные.
        """
        lines = [f"ПРИЧИНА: {_clip(_one_line(self.cause), COMPACT_CAUSE_CHARS)}"
                 + (f" ({UNCONFIRMED_NOTE})" if self.unconfirmed_by_log else "")]
        what = _clip(self.what_first_sentence(), COMPACT_WHAT_CHARS)
        if what:
            lines.append(f"ЧТО СЛОМАЛОСЬ: {what}")
        missing = _clip(self.missing_text, COMPACT_MISSING_CHARS)
        if missing:  # чего не хватает — сводка не должна выдавать причину за установленную
            lines.append(f"НЕ ХВАТАЕТ: {missing}")
        if self.consistency_kind in ("different", "insufficient"):
            # Сводка не должна выдавать неоднородную группу за одну проблему.
            lines.append(f"СОГЛАСОВАННОСТЬ: {_clip(_one_line(self.consistency), COMPACT_MISSING_CHARS)}")
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
    analysis.missing = _join(buckets["missing"])
    analysis.consistency = _join(buckets["consistency"])
    for line in buckets["observations"]:
        if line.strip(" -*•—–.").lower() in ("", "нет", "нету", "отсутствуют"):
            continue  # «НАБЛЮДЕНИЯ: нет» при категории «неизвестно» — значит, наблюдений нет
        observation = parse_observation(line)
        if observation is None:
            analysis.bad_observations.append(line.strip()[:120])
        else:
            analysis.observations.append(observation)
    return analysis


def parse_observation(line: str) -> Observation | None:
    """«- [S3] «цитата»» (после цитаты можно пояснение); ``None`` — не в этом формате."""
    match = _OBSERVATION_RE.match(line)
    if match is None:
        return None
    rest = match.group("rest").strip()
    if not rest or rest[0] not in _OPEN_QUOTES:
        return None
    end = _closing_quote(rest)
    if end is None:
        return None
    quote = rest[1:end].strip()
    return Observation(match.group("id").upper(), quote) if quote else None


def _not_a_quote_mark(text: str, index: int) -> bool:
    """Символ кавычки, который не открывает и не закрывает: ``can't``, ``\\"``."""
    slashes = 0
    while slashes < index and text[index - 1 - slashes] == "\\":
        slashes += 1
    if slashes % 2:  # «\"» экранирована, а «\\"» — слеш, а за ним настоящая кавычка
        return True
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    return before.isalnum() and after.isalnum()


def _closing_quote(text: str) -> int | None:
    """Позиция кавычки, закрывающей цитату, которая открыта первым символом ``text``.

    Кавычки внутри цитаты (``«key "id"»``) и в пояснении после неё
    (``«…» (поле «customer»)``, ``«…» — в `createOrder```) границу не сдвигают:
    закрывающая — парная открывающей и стоит перед концом строки или пояснением.
    """
    opening, closing = text[0], _QUOTE_PAIRS[text[0]]
    symmetric = opening == closing
    candidates: list[int] = []
    depth = 0
    inner = 0  # одинаковых кавычек внутри цитаты (у " ' `)
    for index in range(1, len(text)):
        char = text[index]
        if not symmetric and char == opening:
            depth += 1
        elif char == closing:
            if not symmetric and depth:
                depth -= 1
                continue
            if symmetric and _not_a_quote_mark(text, index):
                continue  # апостроф в слове (can't) или экранированная кавычка
            if inner % 2 == 0 and _AFTER_QUOTE_RE.match(text, index + 1):
                return index
            candidates.append(index)
            inner += 1
    # Пары не сошлись (кавычки разного вида, обрыв) — последняя подходящая кавычка.
    return candidates[-1] if candidates else None


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


def parse_summary(analysis: ClusterAnalysis, task_format: int = 1, examples: int = 1) -> str:
    """Что парсер понял в разборе: подсказка модели, когда формат не принят."""
    parts = []
    for key, title in SECTION_TITLES.items():
        value = getattr(analysis, key)
        part = f"{title} {'✓' if value else '✗'}"
        if key == "cause" and value:
            part += f" (категория: {analysis.category or 'не распознана'})"
        parts.append(part)
        if key == "cause" and task_format >= 2:
            parts.append(f"НАБЛЮДЕНИЯ: {len(analysis.observations)}")
            if analysis.category == "неизвестно":  # обязателен только при «неизвестно»
                parts.append(f"НЕ ХВАТАЕТ {'✓' if analysis.missing_text else '✗'}")
            if examples > 1:
                parts.append(f"СОГЛАСОВАННОСТЬ {'✓' if analysis.consistency_kind else '✗'}")
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
    *,
    task_format: int = 1,
    sources: dict[str, dict[str, Any]] | None = None,
    examples: int = 1,
) -> list[str]:
    """Список проблем формата (пусто — анализ принят).

    ``task_format`` — формат задания кластера (1 — папки до наблюдений). С 2
    нужны наблюдения с цитатами, найденными в своём источнике из ``sources``
    (реестр кластера; ``None`` — реестр не найден или повреждён: наблюдения с ним
    не проверить, это ошибка), а при категории «неизвестно» — содержательный
    «НЕ ХВАТАЕТ».
    """
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
    if task_format >= 2:
        errors.extend(observation_errors(analysis, sources))
        if examples > 1:
            errors.extend(consistency_errors(analysis))
    errors.extend(code_ref_errors(analysis, project_root))
    if analysis.kb_ref and analysis.kb_ref not in offered_kb:
        offered = ", ".join(sorted(offered_kb)) or "в задании записей не было"
        errors.append(
            f"в «БАЗА ЗНАНИЙ:» запись «{analysis.kb_ref}» не предлагалась для этого "
            f"кластера ({offered}) — укажи id из задания или «нет»"
        )
    return errors


MIN_QUOTE_CHARS = 8  # значимых символов (буквы и цифры) в цитате наблюдения
_ELLIPSIS_RE = re.compile(r"…|\.{3,}")
_QUOTE_NORMALIZE = str.maketrans({char: '"' for char in "«»“”„‘’`'"})
OBSERVATION_LINE = "«- [S3] «дословная цитата»»"
REGISTRY_MISSING = (
    "реестр источников этого кластера (evidence/NN.sources.json) не найден или повреждён — "
    "цитаты нельзя проверить. Разбор не меняй: это неполадка скилла, сообщи о ней в блоке "
    "«Проблемы скилла»"
)


def normalize_quote_text(text: str) -> str:
    """Для сверки цитаты: регистр, пробелы и вид кавычек не важны."""
    return " ".join(text.translate(_QUOTE_NORMALIZE).casefold().split())


def quote_found(quote: str, text: str) -> bool:
    """Цитата есть в тексте: части между «…» идут в нём по порядку."""
    haystack = normalize_quote_text(text)
    position = 0
    for part in quote_parts(quote):
        found = haystack.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    return True


def quote_parts(quote: str) -> list[str]:
    """Части цитаты между «…» без пробелов и знаков препинания по краям."""
    parts = (normalize_quote_text(part).strip(" .,;:") for part in _ELLIPSIS_RE.split(quote))
    return [part for part in parts if part]


def consistency_errors(analysis: ClusterAnalysis) -> list[str]:
    """«СОГЛАСОВАННОСТЬ» обязательна, когда в задании несколько примеров."""
    options = "«одна причина», «разные проблемы — <чем отличаются примеры>» или «недостаточно данных»"
    if not analysis.consistency:
        return [f"в данных несколько примеров — добавь «СОГЛАСОВАННОСТЬ:» {options}"]
    if analysis.consistency_kind is None:
        return [f"в «СОГЛАСОВАННОСТЬ:» напиши одно из: {options}"]
    if analysis.consistency_kind == "different" and len(analysis.consistency_detail) < 10:
        return ["в «СОГЛАСОВАННОСТЬ: разные проблемы — …» после «—» назови, чем отличаются примеры"]
    return []


def observation_errors(
    analysis: ClusterAnalysis,
    sources: dict[str, dict[str, Any]] | None,
) -> list[str]:
    """Проблемы «НАБЛЮДЕНИЯ» и «НЕ ХВАТАЕТ» разбора нового формата.

    Совпадение цитаты подтверждает наблюдение, а не причинную связь: проверяется
    только, что строка действительно есть в названном куске данных.
    """
    errors: list[str] = []
    unknown = analysis.category == "неизвестно"
    for line in analysis.bad_observations:
        errors.append(
            f"в «НАБЛЮДЕНИЯ:» строка «{line[:80]}» не в формате {OBSERVATION_LINE}: id куска "
            "данных из задания в скобках и цитата в кавычках"
        )
    if not analysis.observations and not unknown:
        errors.append(
            "нет раздела «НАБЛЮДЕНИЯ:» с цитатами — добавь 1–3 строки "
            f"{OBSERVATION_LINE} из «Данных» задания (без наблюдений можно только с "
            "категорией «неизвестно» и «НЕ ХВАТАЕТ:»)"
        )
    if unknown and not analysis.missing_text:
        errors.append(
            "при категории «неизвестно» в «НЕ ХВАТАЕТ:» напиши, каких данных нет и какая "
            "проверка различит версии причины"
        )
    if sources is None:
        if analysis.observations:
            errors.append(REGISTRY_MISSING)
        return errors
    known = ", ".join(sorted(sources, key=lambda key: int(key[1:]) if key[1:].isdigit() else 0))
    for observation in analysis.observations:
        source_id, quote = observation.source_id, observation.quote
        short = quote if len(quote) <= 80 else quote[:79] + "…"
        record = sources.get(source_id)
        if record is None:
            errors.append(
                f"в «НАБЛЮДЕНИЯ:» источника {source_id} нет в задании "
                f"({'есть ' + known if known else 'кусков данных в задании нет'})"
            )
            continue
        significant = sum(char.isalnum() for part in quote_parts(quote) for char in part)
        if significant < MIN_QUOTE_CHARS:
            errors.append(
                f"в «НАБЛЮДЕНИЯ:» цитата «{short}» слишком короткая — возьми из {source_id} "
                f"строку не короче {MIN_QUOTE_CHARS} букв и цифр"
            )
            continue
        if quote_found(quote, str(record.get("text") or "")):
            continue
        elsewhere = [key for key, other in sources.items()
                     if key != source_id and quote_found(quote, str(other.get("text") or ""))]
        if elsewhere:
            errors.append(
                f"в «НАБЛЮДЕНИЯ:» цитата «{short}» есть в {elsewhere[0]}, а не в {source_id} — "
                f"укажи [{elsewhere[0]}]"
            )
        else:
            errors.append(
                f"в «НАБЛЮДЕНИЯ:» цитаты «{short}» нет в {source_id} — скопируй строку из этого "
                "куска «Данных» задания дословно (пропуск внутри цитаты — «…»)"
            )
    return errors


def _looks_like_file_ref(raw: str) -> bool:
    normalized = raw.replace("\\", "/")
    return "/" in normalized or PurePath(normalized).suffix.lower() in (
        SOURCE_EXTENSIONS | _CONFIG_EXTENSIONS
    )


def _code_location(text: str, root: Path) -> tuple[str, str | None] | None:
    """Новые формы пути с пробелами, затем прежний поиск ссылки среди текста."""
    for quoted in _QUOTED_LOCATION_RE.finditer(text):
        location = _LOCATION_RE.fullmatch(quoted.group("content"))
        if location is None or not _looks_like_file_ref(location.group("path")):
            continue
        # Сохраняем первый legacy match, включая вызов метода с именем фикстуры.
        if _PATH_RE.search(text[:quoted.start()]):
            break
        return location.group("path"), location.group("line") or quoted.group("line")

    # Выбираем самый длинный существующий начальный путь. Описание, начинающееся
    # после имени файла, не должно стать частью пути, а несколько точек — обрезать его.
    for ending in reversed(list(_LOCATION_END_RE.finditer(text))):
        location = _LOCATION_RE.fullmatch(text[:ending.end()].strip())
        if location is None or not _looks_like_file_ref(location.group("path")):
            continue
        raw = location.group("path").replace("\\", "/")
        if not any(char.isspace() for char in raw):
            continue  # для обычного пути сохраняем прежний поиск без лишнего stat
        candidate = Path(raw)
        try:
            path = candidate if candidate.is_absolute() else root / candidate
            if path.resolve().is_file():
                # Принадлежность проекту проверяется позже: внешний существующий
                # путь нельзя заменить внутренним basename через запасной поиск.
                return raw, location.group("line")
        except (OSError, ValueError):
            continue

    legacy = _PATH_RE.search(text)
    return (legacy.group("path"), legacy.group("line")) if legacy else None


def code_ref_errors(analysis: ClusterAnalysis, project_root: Path) -> list[str]:
    """Проблемы ссылок из раздела КОД: нет файла в проекте или строка вне файла."""
    errors: list[str] = []
    root = project_root.resolve()
    for line in analysis.code:
        location = _code_location(line, root)
        if location is None:
            continue
        raw, number = location
        raw = raw.replace("\\", "/")
        suffix = PurePath(raw).suffix.lower()
        if not _looks_like_file_ref(raw):
            continue  # похоже на вызов метода (orderApi.create), а не на файл
        candidate = Path(raw)
        path = candidate if candidate.is_absolute() else root / candidate
        try:
            resolved = path.resolve()
            inside = resolved == root or root in resolved.parents
        except (OSError, ValueError):
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
                try:
                    resolved = (root / found[0]).resolve()
                    inside = resolved == root or root in resolved.parents
                except (OSError, ValueError):
                    inside = False
        if not inside or not resolved.is_file():
            errors.append(
                f"в «КОД:» файл «{raw}» не найден в проекте {root} — "
                "укажи путь относительно корня проекта или удали строку КОД"
            )
            continue
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

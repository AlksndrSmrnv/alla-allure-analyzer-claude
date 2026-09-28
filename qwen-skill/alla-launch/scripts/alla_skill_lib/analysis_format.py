"""Разбор и лёгкая проверка анализа кластера, который написал агент.

Формат тот же, что серверный промпт требует от LLM::

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

from alla_skill_lib.code_hints import SOURCE_EXTENSIONS

CATEGORIES = ("тест", "приложение", "окружение", "данные", "неизвестно")
_CATEGORY_ALIASES = {
    "тестовые данные": "данные",
    "не определено": "неизвестно",
    "автотест": "тест",
    "тест": "тест",
    "test": "тест",
    "приложение": "приложение",
    "продукт": "приложение",
    "сервис": "приложение",
    "бэкенд": "приложение",
    "service": "приложение",
    "app": "приложение",
    "окружение": "окружение",
    "инфраструктура": "окружение",
    "стенд": "окружение",
    "env": "окружение",
    "environment": "окружение",
    "данные": "данные",
    "data": "данные",
    "неизвестно": "неизвестно",
    "unknown": "неизвестно",
}
_SECTIONS = {
    "что сломалось": "what",
    "причина": "cause",
    "как исправить": "fix",
    "код": "code",
    "база знаний": "kb",
    "название": "title",
    "признак": "fingerprint",
}
_HEADER_RE = re.compile(
    r"^(что сломалось|причина|как исправить|код|база знаний|название|признак)"
    r"\s*(?:[:：]\s*(.*)|$)",
    re.IGNORECASE,
)
_NO_KB = {"нет", "-", "—", "none", "no"}
_DECOR_RE = re.compile(r"^[\s#>*_`-]+")
_PATH_RE = re.compile(r"(?P<path>[\w.\-/\\]+\.[A-Za-z0-9]{1,8})(?::(?P<line>\d+))?")
_CONFIG_EXTENSIONS = frozenset({
    ".yaml", ".yml", ".json", ".xml", ".properties", ".conf", ".cfg", ".ini",
    ".toml", ".gradle", ".sql", ".md", ".txt", ".env", ".csv",
})
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")
_STEP_PREFIX_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
MAX_CHECKED_FILE_BYTES = 5_000_000


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

    @property
    def cause_reason(self) -> str:
        """Текст ПРИЧИНЫ без категории в начале."""
        text = _strip_category(self.cause)
        return text or self.cause

    def compact(self) -> str:
        """Сжатый разбор для задания на общий анализ.

        Причина с категорией, первое предложение «что сломалось» и первый шаг
        исправления — из них сводка собирает ключевые проблемы и приоритетные
        исправления, не тратя контекст на полные разборы.
        """
        lines = [f"ПРИЧИНА: {_one_line(self.cause)}"]
        what = _SENTENCE_END_RE.split(_one_line(self.what), maxsplit=1)[0]
        if what:
            lines.append(f"ЧТО СЛОМАЛОСЬ: {what}")
        step = self.first_fix_step()
        if step:
            lines.append(f"ПЕРВЫЙ ШАГ ИСПРАВЛЕНИЯ: {step}")
        return "\n".join(lines)

    def first_fix_step(self) -> str:
        for line in self.fix.splitlines():
            step = _STEP_PREFIX_RE.sub("", line).strip()
            if step:
                return step
        return ""


def parse_analysis(text: str) -> ClusterAnalysis:
    """Разобрать текст анализа на разделы (неизвестный текст до разделов игнорируется)."""
    analysis = ClusterAnalysis(raw=text.strip())
    buckets: dict[str, list[str]] = {key: [] for key in _SECTIONS.values()}
    current: str | None = None
    for raw_line in text.splitlines():
        cleaned = _DECOR_RE.sub("", raw_line.replace("**", "").replace("__", "")).strip()
        header = _HEADER_RE.match(cleaned)
        if header:
            current = _SECTIONS[header.group(1).lower()]
            rest = (header.group(2) or "").strip()
            if rest:
                buckets[current].append(rest)
            continue
        if current is not None:
            buckets[current].append(raw_line.rstrip())

    analysis.what = _join(buckets["what"])
    analysis.cause = _join(buckets["cause"])
    analysis.fix = _join(buckets["fix"])
    analysis.code = [line.strip(" -*") for line in buckets["code"] if line.strip(" -*")]
    analysis.category = detect_category(analysis.cause)
    analysis.kb_ref = _kb_ref(_join(buckets["kb"]))
    analysis.title = _one_line(_join(buckets["title"]))
    analysis.fingerprint = _join(buckets["fingerprint"])
    return analysis


def _kb_ref(value: str) -> str | None:
    """id записи из строки «БАЗА ЗНАНИЙ:»; «нет»/«-» — None."""
    words = value.strip().strip("`[]«»\"'()").split()
    if not words or words[0].lower().strip(".,") in _NO_KB:
        return None
    return words[0].strip("`[]«»\"'().,;:").lower()


def detect_category(cause: str) -> str | None:
    """Категория из начала раздела ПРИЧИНА: ``приложение — ...``, ``[тест] ...``."""
    head = cause.strip().lstrip("[«\"'(*`").lower()
    for alias in sorted(_CATEGORY_ALIASES, key=len, reverse=True):
        if head.startswith(alias):
            following = head[len(alias):len(alias) + 1]
            if not following or not following.isalnum():
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
            "в «ПРИЧИНА:» первым словом должна идти категория: "
            + " / ".join(CATEGORIES)
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
    head = cause.strip().lstrip("[«\"'(*`")
    for alias in sorted(_CATEGORY_ALIASES, key=len, reverse=True):
        if head.lower().startswith(alias):
            head = head[len(alias):]
            break
    return head.lstrip(" ]»\"')*`—–-:").strip()


def _join(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def _one_line(text: str) -> str:
    return " ".join(text.split())

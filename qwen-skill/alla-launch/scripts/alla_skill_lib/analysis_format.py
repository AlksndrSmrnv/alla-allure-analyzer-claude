"""Разбор и лёгкая проверка анализа кластера, который написал агент.

Формат тот же, что серверный промпт требует от LLM::

    ЧТО СЛОМАЛОСЬ: ...
    ПРИЧИНА: <тест|приложение|окружение|данные|неизвестно> — ...
    КАК ИСПРАВИТЬ:
    1. ...
    КОД: path/to/Test.java:42 — ...   (необязательно)

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
}
_HEADER_RE = re.compile(
    r"^(что сломалось|причина|как исправить|код)\s*(?:[:：]\s*(.*)|$)",
    re.IGNORECASE,
)
_DECOR_RE = re.compile(r"^[\s#>*_`-]+")
_PATH_RE = re.compile(r"(?P<path>[\w.\-/\\]+\.[A-Za-z0-9]{1,8})(?::(?P<line>\d+))?")
_CONFIG_EXTENSIONS = frozenset({
    ".yaml", ".yml", ".json", ".xml", ".properties", ".conf", ".cfg", ".ini",
    ".toml", ".gradle", ".sql", ".md", ".txt", ".env", ".csv",
})
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")


@dataclass
class ClusterAnalysis:
    """Разобранный анализ кластера."""

    raw: str
    what: str = ""
    cause: str = ""
    category: str | None = None
    fix: str = ""
    code: list[str] = field(default_factory=list)

    @property
    def cause_reason(self) -> str:
        """Текст ПРИЧИНЫ без категории в начале."""
        text = _strip_category(self.cause)
        return text or self.cause

    def compact(self) -> str:
        """Короткая версия для задания на общий анализ большого прогона."""
        what = _SENTENCE_END_RE.split(self.what.strip(), maxsplit=1)[0] if self.what else ""
        lines = [f"ПРИЧИНА: {self.cause}".strip()]
        if what:
            lines.append(f"ЧТО СЛОМАЛОСЬ: {what}")
        return "\n".join(lines)


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
    return analysis


def detect_category(cause: str) -> str | None:
    """Категория из начала раздела ПРИЧИНА: ``приложение — ...``, ``[тест] ...``."""
    head = cause.strip().lstrip("[«\"'(*`").lower()
    for alias in sorted(_CATEGORY_ALIASES, key=len, reverse=True):
        if head.startswith(alias):
            following = head[len(alias):len(alias) + 1]
            if not following or not following.isalnum():
                return _CATEGORY_ALIASES[alias]
    return None


def validate_analysis(analysis: ClusterAnalysis, project_root: Path) -> list[str]:
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
    for missing in missing_code_paths(analysis, project_root):
        errors.append(
            f"в «КОД:» файл «{missing}» не найден в проекте {project_root} — "
            "укажи путь относительно корня проекта или удали строку КОД"
        )
    return errors


def missing_code_paths(analysis: ClusterAnalysis, project_root: Path) -> list[str]:
    """Пути из раздела КОД, которых нет в проекте."""
    missing: list[str] = []
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
            missing.append(raw)
    return missing


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

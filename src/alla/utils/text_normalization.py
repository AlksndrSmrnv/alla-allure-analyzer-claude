"""Нормализация текста — замена волатильных данных плейсхолдерами.

Используется в кластеризации и KB-matching для устранения различий
в UUID, timestamps, числах, IP-адресах между запусками.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_UUID_NOHYPHEN_RE = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)

# --- Даты и время (от более специфичных к менее специфичным) ---

# ISO 8601 полный datetime + опциональные секунды, millis/micros и timezone.
# Ловит HH:MM и HH:MM:SS, а также Java/Log4j запятую: 2026-02-06 10:12:13,123
_DATETIME_ISO_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
    r"(?::\d{2})?"
    r"(?:[.,]\d{1,6})?"
    r"(?:Z|[+-]\d{2}:?\d{2})?"
)

# Именованные месяцы (EN): "Feb 6, 2026", "06 Feb 2026", "6-Feb-2026"
# + опциональное время после даты.
_MONTH_NAMES = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DATETIME_NAMED_MONTH_RE = re.compile(
    r"(?:"
    r"\d{1,2}[- ]" + _MONTH_NAMES + r"[- ]\d{4}"
    r"|"
    + _MONTH_NAMES + r"\.?\s+\d{1,2},?\s+\d{4}"
    r")"
    r"(?:[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)?",
    re.IGNORECASE,
)

# Слэш-даты: 02/06/2026, 2026/02/06 (требуется 4-значный год)
_DATE_SLASH_RE = re.compile(
    r"\b\d{4}/\d{1,2}/\d{1,2}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{4}\b"
)

# Точка-даты: 06.02.2026, 2026.02.06 (требуется 4-значный год → не ловит версии)
_DATE_DOT_RE = re.compile(
    r"\b\d{4}\.\d{1,2}\.\d{1,2}\b"
    r"|\b\d{1,2}\.\d{1,2}\.\d{4}\b"
)

# ISO дата без времени: 2026-02-06
_DATE_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b(?![T ]\d{2}:\d{2}:\d{2})")

# Standalone время: 10:12:13, 10:12:13.123, 10:12:13,456
_TIME_ONLY_RE = re.compile(
    r"(?<!\d[.:])\b\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?\b"
)

_LONG_NUMBER_RE = re.compile(r"\b\d{4,}\b")
_IP_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")


def normalize_text(text: str) -> str:
    """Заменить волатильные данные плейсхолдерами.

    Не трогаем саму структуру текста, не удаляем стоп-слова,
    не приводим к lowercase — всё это делает TfidfVectorizer.

    Порядок применения критичен:
    - UUID до дат (hex-UUID содержит цифры, похожие на даты)
    - Полный datetime до date-only (иначе дата матчится отдельно от времени)
    - IP до точка-дат (192.168.1.1 не должен стать <TS>)
    - Long numbers последними (иначе год «2026» станет <NUM> до матча даты)
    """
    text = _UUID_RE.sub("<ID>", text)
    text = _UUID_NOHYPHEN_RE.sub("<ID>", text)
    text = _DATETIME_ISO_RE.sub("<TS>", text)
    text = _DATETIME_NAMED_MONTH_RE.sub("<TS>", text)
    text = _IP_RE.sub("<IP>", text)
    text = _DATE_SLASH_RE.sub("<TS>", text)
    text = _DATE_DOT_RE.sub("<TS>", text)
    text = _DATE_ISO_RE.sub("<TS>", text)
    text = _TIME_ONLY_RE.sub("<TS>", text)
    text = _LONG_NUMBER_RE.sub("<NUM>", text)
    return text

"""Вычисление fingerprint текста ошибки для системы обратной связи KB."""

from __future__ import annotations

import hashlib

from alla.utils.text_normalization import normalize_text

FINGERPRINT_VERSION = 1
"""Версия алгоритма нормализации.

Включается в хэш: ``sha256(f"v{VERSION}:{normalized}")``.
При изменении ``normalize_text()`` инкрементировать — старые fingerprint'ы
в БД тихо перестанут матчиться (graceful degradation, не ломают, не дают
ложных exclusion'ов). Migration-скрипт может пересчитать.
"""


def compute_fingerprint(error_text: str) -> str:
    """SHA-256 hex digest нормализованного полного текста ошибки.

    Принимает полный error_text (message + trace + logs) — тот же текст,
    что используется для KB-поиска. Это обеспечивает точную дискриминацию:
    одинаковые message с разными trace/logs дают разные fingerprint'ы.

    Стабильность: после нормализации (UUID/TS/NUM/IP → placeholders)
    fingerprint одинаков между прогонами при неизменном коде.
    При изменении кода trace меняется → fingerprint меняется →
    старый feedback тихо перестаёт матчиться (переголосование оправдано).

    Returns:
        64-символьная hex-строка (SHA-256).
    """
    normalized = normalize_text(error_text)
    payload = f"v{FINGERPRINT_VERSION}:{normalized}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

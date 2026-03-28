"""Вспомогательные функции для нормализации и сравнения путей шагов."""

import re

from alla.utils.text_normalization import normalize_text

_MULTI_WS_RE = re.compile(r"\s+")
_STEP_SPLIT_RE = re.compile(r"\s*(?:→|->)\s*")


def _collapse_whitespace(text: str) -> str:
    return _MULTI_WS_RE.sub(" ", text).strip()


def split_normalized_step_path(step_path: str | None) -> list[str]:
    """Разбить breadcrumb шага на нормализованные сегменты."""
    if not step_path:
        return []

    normalized = normalize_text(step_path)
    parts = [
        _collapse_whitespace(part).casefold()
        for part in _STEP_SPLIT_RE.split(normalized)
    ]
    return [part for part in parts if part]


def normalize_step_path(step_path: str | None) -> str:
    """Нормализовать breadcrumb шага в стабильный канонический вид."""
    return " → ".join(split_normalized_step_path(step_path))


def are_step_paths_compatible(
    entry_step_path: str | None,
    query_step_path: str | None,
) -> bool:
    """Проверить совместимость двух step_path.

    Совместимыми считаются:
    - полностью одинаковые пути;
    - пути, где один является suffix другого.
    """
    entry_parts = split_normalized_step_path(entry_step_path)
    query_parts = split_normalized_step_path(query_step_path)
    if not entry_parts or not query_parts:
        return False

    if entry_parts == query_parts:
        return True

    shorter, longer = (
        (entry_parts, query_parts)
        if len(entry_parts) <= len(query_parts)
        else (query_parts, entry_parts)
    )
    return shorter == longer[-len(shorter):]

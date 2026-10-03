"""Вспомогательные функции для нормализации и сравнения путей шагов."""

import re

from alla_core.utils.text_normalization import normalize_text

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

"""Utilities for compact one-line previews in debug logs."""

from __future__ import annotations


def preview_head(text: str, max_chars: int) -> str:
    """Return a single-line preview from the beginning of the text."""
    return text[:max_chars].replace("\n", " ")


def preview_tail(text: str, max_chars: int) -> str:
    """Return a single-line preview from the end of the text."""
    if len(text) <= max_chars:
        return text.replace("\n", " ")
    return text[-max_chars:].replace("\n", " ")

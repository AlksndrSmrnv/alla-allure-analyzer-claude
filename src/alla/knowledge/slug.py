"""Stable slug generation for KB entries."""

from __future__ import annotations

import hashlib
import re

from alla.utils.step_paths import normalize_step_path


def make_kb_slug(title: str, error_example: str, step_path: str | None = None) -> str:
    """Generate the canonical KB slug used by all REST entrypoints."""
    base = re.sub(r"[^a-z0-9]+", "_", title.lower())
    base = base.strip("_")[:50] or "kb_entry"
    signature_material = error_example
    normalized_step_path = normalize_step_path(step_path)
    if normalized_step_path:
        signature_material = f"{error_example}\n---\n{normalized_step_path}"
    suffix = hashlib.sha256(signature_material.encode()).hexdigest()[:8]
    return f"{base}_{suffix}"

"""Вендоренное ядро Qwen-скилла alla-launch не расходится с src/alla."""

import ast
import importlib.util
from pathlib import Path

from alla.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "sync_qwen_skill", REPO_ROOT / "tools" / "sync_qwen_skill.py"
)
assert _spec is not None and _spec.loader is not None
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


def test_vendored_core_matches_sources() -> None:
    issues = sync.check()
    assert issues == [], (
        "alla_core устарел — выполните `python tools/sync_qwen_skill.py`:\n"
        + "\n".join(issues)
    )


def test_render_rewrites_only_import_statements() -> None:
    source = (
        '"""Упоминание alla.config в docstring."""\n'
        "import logging\n"
        "from alla.models import x\n"
        "import alla.utils\n"
        "def f():\n"
        "    from alla.services.y import z\n"
        "    return 'alla.config'\n"
    )
    rendered = sync.render_module("demo.py", source)
    assert "from alla_core.models import x" in rendered
    assert "import alla_core.utils" in rendered
    assert "    from alla_core.services.y import z" in rendered
    assert "Упоминание alla.config" in rendered
    assert "'alla.config'" in rendered


def test_shim_defaults_match_server_settings() -> None:
    tree = ast.parse((sync.CORE_ROOT / "config.py").read_text(encoding="utf-8"))
    settings_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )
    checked = 0
    for item in settings_class.body:
        if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
            continue
        name = item.target.id
        if name in {"endpoint", "token"}:
            continue
        default = eval(compile(ast.Expression(item.value), "config.py", "eval"), {})
        assert default == Settings.model_fields[name].default, name
        checked += 1
    assert checked >= 10


def test_shim_bounds_match_server_settings() -> None:
    """Границы ge/le в shim те же, что у Field(...) серверного Settings."""
    tree = ast.parse((sync.CORE_ROOT / "config.py").read_text(encoding="utf-8"))
    bounds_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "BOUNDS"
    )
    shim_bounds = ast.literal_eval(bounds_node.value)

    server_bounds = {}
    for name in sync.shim_fields() - {"endpoint", "token"}:
        ge = le = None
        for constraint in Settings.model_fields[name].metadata:
            ge = getattr(constraint, "ge", ge)
            le = getattr(constraint, "le", le)
        if ge is not None or le is not None:
            server_bounds[name] = (ge, le)

    assert shim_bounds == server_bounds

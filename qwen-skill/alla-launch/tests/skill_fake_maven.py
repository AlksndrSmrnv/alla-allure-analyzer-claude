"""Фейковый Maven для перезапуска тестов: ``mvnw`` проекта пишет отчёты surefire по сценарию.

``install`` кладёт в папку исполняемый ``mvnw`` и сценарий ``fake-maven.json``: для каждого
``класс#метод`` — итоги запусков по порядку (``"failed:<сообщение>"``, ``"passed"``,
``"skipped"``, ``"flaky"``; последний повторяется). Скрипт разбирает ``-Dtest``, ``-pl`` и
``-Dsurefire.reportNameSuffix``, как настоящий surefire пишет ``TEST-<класс>-<суффикс>.xml``,
и считает запуски в ``fake-maven-runs.json``. Сам по себе запускается как
``python skill_fake_maven.py --spec <файл> <аргументы mvn>``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

SPEC_NAME = "fake-maven.json"
RUNS_NAME = "fake-maven-runs.json"


def install(
    folder: Path,
    results: dict[str, list[str]],
    *,
    exit_code: int = 0,
    sleep: float = 0.0,
    not_in_reactor: bool = False,
    python: str = sys.executable,
    script: str = str(Path(__file__).resolve()),
) -> Path:
    """Положить ``mvnw`` и сценарий в ``folder``; вернуть путь к ``mvnw``.

    ``python`` и ``script`` — для shell: стенд Qwen передаёт пути от папки ``mvnw``
    (``$(dirname "$0")/…``), чтобы песочница не читала ничего вне проекта.
    """
    spec = folder / SPEC_NAME
    spec.write_text(json.dumps({
        "results": results, "exit": exit_code, "sleep": sleep, "not_in_reactor": not_in_reactor,
    }), encoding="utf-8")
    wrapper = folder / "mvnw"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{python}" "{script}" --spec "{spec}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


def runs(folder: Path) -> dict[str, int]:
    """Сколько раз запускался каждый ``класс#метод``."""
    path = folder / RUNS_NAME
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def main(argv: list[str]) -> int:
    spec_path = Path(argv[argv.index("--spec") + 1])
    spec: dict[str, Any] = json.loads(spec_path.read_text(encoding="utf-8"))
    args = [arg for arg in argv if arg not in ("--spec", str(spec_path))]
    print("[INFO] Scanning for projects...", flush=True)
    if spec["sleep"]:
        time.sleep(float(spec["sleep"]))
    module_dir = Path.cwd()
    if "-pl" in args:
        if spec["not_in_reactor"]:
            print(f"[ERROR] Could not find the selected project in the reactor: {args[args.index('-pl') + 1]}")
            return 1
        module_dir = module_dir / args[args.index("-pl") + 1]
    if spec["exit"]:
        print("[ERROR] COMPILATION ERROR :")
        print("[ERROR] /src/test/java/ru/company/orders/OrderTest.java:[3,1] cannot find symbol")
        return int(spec["exit"])
    props: dict[str, str] = {}
    for arg in args:
        if arg.startswith("-D"):
            key, _, value = arg[2:].partition("=")
            props[key] = value
    suffix = props.get("surefire.reportNameSuffix", "")
    reports = module_dir / "target" / "surefire-reports"
    reports.mkdir(parents=True, exist_ok=True)
    counts = runs(spec_path.parent)
    for selector in props.get("test", "").split(","):
        cls, _, methods = selector.partition("#")
        cases: list[str] = []
        for method in methods.split("+"):
            key = f"{cls}#{method}"
            plan = spec["results"].get(key)
            if not plan:
                continue
            outcome = plan[min(counts.get(key, 0), len(plan) - 1)]
            counts[key] = counts.get(key, 0) + 1
            cases.append(_case(cls, suffix, method, outcome))
        if cases:
            name = f"TEST-{cls}-{suffix}.xml" if suffix else f"TEST-{cls}.xml"
            (reports / name).write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<testsuite name={quoteattr(cls)} tests="{len(cases)}">\n'
                + "".join(cases) + "</testsuite>\n",
                encoding="utf-8",
            )
    (spec_path.parent / RUNS_NAME).write_text(json.dumps(counts), encoding="utf-8")
    print("[INFO] BUILD SUCCESS")
    return 0


def _case(cls: str, suffix: str, method: str, outcome: str) -> str:
    classname = quoteattr(f"{cls}({suffix})" if suffix else cls)
    head = f"  <testcase name={quoteattr(method)} classname={classname}"
    if outcome.startswith("failed:"):
        message = quoteattr(outcome.partition(":")[2])
        return f'{head}>\n    <failure message={message} type="AssertionError">trace</failure>\n  </testcase>\n'
    if outcome == "skipped":
        return f"{head}>\n    <skipped/>\n  </testcase>\n"
    if outcome == "flaky":
        return f'{head}>\n    <flakyFailure message="first" type="AssertionError"/>\n  </testcase>\n'
    return f"{head}/>\n"


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Repository entry point for the self-contained alla-launch quality harness."""

from pathlib import Path
import runpy


if __name__ == "__main__":
    harness = (Path(__file__).resolve().parents[1] / "qwen-skill" / "alla-launch"
               / "tests" / "quality_harness.py")
    runpy.run_path(str(harness), run_name="__main__")

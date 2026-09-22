#!/usr/bin/env python3
"""Entry point; paths are resolved independently of the skill's install directory."""

import argparse
import asyncio
import json
import logging
from pathlib import Path

from alla_skill.client import Client
from alla_skill.config import Settings
from alla_skill.evidence import redact
from alla_skill.workflow import context, finalize, prepare


def main():
    parser = argparse.ArgumentParser(description="Подготовка данных и отчёта для скилла Alla")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("prepare")
    collect.add_argument("launch_id", type=int)
    collect.add_argument("--project-root", type=Path, default=Path.cwd())
    inspect = commands.add_parser("context")
    inspect.add_argument("run_dir", type=Path)
    inspect.add_argument("cluster_id", nargs="?")
    inspect.add_argument("--member-id", type=int)
    inspect.add_argument("--source")
    inspect.add_argument("--offset", type=int, default=0)
    inspect.add_argument("--limit", type=int, default=8000)
    finish = commands.add_parser("finalize")
    finish.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    # Evidence can contain secrets, so third-party/data debug logs stay disabled.
    logging.disable(logging.CRITICAL)
    try:
        if args.command == "prepare":

            async def collect_run():
                async with Client(Settings.load(args.project_root)) as client:
                    directory = await prepare(args.launch_id, args.project_root, client)
                    return context(directory)

            result = asyncio.run(collect_run())
        elif args.command == "context":
            result = context(
                args.run_dir,
                args.cluster_id,
                member_id=args.member_id,
                source=args.source,
                offset=args.offset,
                limit=args.limit,
            )
        else:
            result = finalize(args.run_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": redact(str(exc))}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

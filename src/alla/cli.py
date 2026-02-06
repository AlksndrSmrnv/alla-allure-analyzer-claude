"""CLI entry point for the alla triage agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from alla import __version__

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alla",
        description="AI Test Failure Triage Agent — analyze failed tests from Allure TestOps",
    )
    parser.add_argument(
        "launch_id",
        nargs="?",
        type=int,
        help="Launch ID to analyze (overrides ALLURE_LAUNCH_ID env var if set)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Logging verbosity (overrides ALLURE_LOG_LEVEL)",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=None,
        help="Results per page (overrides ALLURE_PAGE_SIZE)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"alla {__version__}",
    )
    return parser


async def async_main(args: argparse.Namespace) -> int:
    """Wire up dependencies and run triage. Returns exit code."""
    # Import here to defer heavy imports and keep --help fast
    from alla.clients.auth import AllureAuthManager
    from alla.clients.testops_client import AllureTestOpsClient
    from alla.config import Settings
    from alla.exceptions import AllaError, ConfigurationError
    from alla.logging_config import setup_logging
    from alla.services.triage_service import TriageService

    # 1. Load settings
    try:
        overrides: dict[str, object] = {}
        if args.page_size is not None:
            overrides["page_size"] = args.page_size
        settings = Settings(**overrides)  # type: ignore[arg-type]
    except Exception as exc:
        # pydantic-settings raises ValidationError for missing fields
        print(
            f"Configuration error: {exc}\n\n"
            f"Required env vars: ALLURE_ENDPOINT, ALLURE_TOKEN, ALLURE_PROJECT_ID\n"
            f"See .env.example for details.",
            file=sys.stderr,
        )
        return 2

    # 2. Setup logging
    log_level = args.log_level or settings.log_level
    setup_logging(log_level)

    # 3. Determine launch ID
    launch_id = args.launch_id
    if launch_id is None:
        logger.error(
            "No launch_id provided. Pass it as a positional argument: alla <launch_id>"
        )
        return 2

    # 4. Run triage
    auth = AllureAuthManager(
        endpoint=settings.endpoint,
        api_token=settings.token,
        timeout=settings.request_timeout,
        ssl_verify=settings.ssl_verify,
    )

    try:
        async with AllureTestOpsClient(settings, auth) as client:
            service = TriageService(client, settings)
            report = await service.analyze_launch(launch_id)
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        return 2
    except AllaError as exc:
        logger.error("Error: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130

    # 5. Output report
    if args.output_format == "json":
        print(report.model_dump_json(indent=2))
    else:
        _print_text_report(report)

    return 0


def _print_text_report(report: TriageReport) -> None:  # noqa: F821
    """Print a human-readable triage report to stdout."""

    print()
    print("=== Allure Triage Report ===")
    launch_label = f"Launch: #{report.launch_id}"
    if report.launch_name:
        launch_label += f" ({report.launch_name})"
    print(launch_label)
    print(
        f"Total: {report.total_results}"
        f" | Passed: {report.passed_count}"
        f" | Failed: {report.failed_count}"
        f" | Broken: {report.broken_count}"
        f" | Skipped: {report.skipped_count}"
        f" | Unknown: {report.unknown_count}"
    )
    print()

    if report.failed_tests:
        print(f"Failures ({report.failure_count}):")
        for t in report.failed_tests:
            print(f"  [{t.status.value.upper()}]  {t.name} (ID: {t.test_result_id})")
            if t.link:
                print(f"            {t.link}")
            if t.status_message:
                # Truncate long messages
                msg = t.status_message
                if len(msg) > 200:
                    msg = msg[:200] + "..."
                print(f"            {msg}")
    else:
        print("No failures found.")

    print()


def main() -> None:
    """Sync entry point for the CLI."""
    parser = build_parser()
    args = parser.parse_args()
    exit_code = asyncio.run(async_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

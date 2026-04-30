#!/usr/bin/env python3
"""Run read-only Alla launch analysis through alla-server REST endpoints.

This helper intentionally uses only Python's standard library. It intentionally
does not read environment variables: edit ALLA_SERVER_URL below.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ALLA_SERVER_URL = "https://TODO-ALLA-SERVER"
REQUEST_TIMEOUT_SECONDS = 180
MAX_TEXT_CHARS = 900
MAX_MATCHES_PER_CLUSTER = 3


class AllaRequestError(RuntimeError):
    """HTTP or network failure returned in a structured form."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        detail: Any = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.url = url

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": str(self),
        }
        if self.status is not None:
            payload["status"] = self.status
        if self.detail is not None:
            payload["detail"] = self.detail
        if self.url is not None:
            payload["url"] = self.url
        return payload


def _ensure_configured() -> str:
    base_url = ALLA_SERVER_URL.strip().rstrip("/")
    if not base_url or "TODO-ALLA-SERVER" in base_url:
        raise AllaRequestError(
            "ALLA_SERVER_URL is still a placeholder. Edit scripts/run_alla_analysis.py."
        )
    return base_url


def _truncate(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _json_or_text(raw: bytes, content_type: str | None) -> Any:
    text = raw.decode("utf-8", errors="replace")
    if content_type and "application/json" in content_type.lower():
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return text
    return text


def _request(
    method: str,
    url: str,
    *,
    expect_json: bool = True,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> tuple[Any, dict[str, str]]:
    request = Request(url, method=method, headers={"Accept": "application/json"})
    if method == "POST":
        request.add_header("Content-Length", "0")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            headers = dict(response.headers.items())
            if expect_json:
                return _json_or_text(raw, response.headers.get("Content-Type")), headers
            return raw.decode("utf-8", errors="replace"), headers
    except HTTPError as exc:
        raw = exc.read()
        detail = _json_or_text(raw, exc.headers.get("Content-Type")) if raw else None
        raise AllaRequestError(
            f"Alla server returned HTTP {exc.code}",
            status=exc.code,
            detail=detail,
            url=url,
        ) from exc
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise AllaRequestError(
                f"Alla server request timed out after {timeout}s",
                url=url,
            ) from exc
        raise AllaRequestError(
            f"Cannot reach Alla server: {exc.reason}",
            url=url,
        ) from exc
    except TimeoutError as exc:
        raise AllaRequestError(
            f"Alla server request timed out after {timeout}s",
            url=url,
        ) from exc


def _with_query(url: str, params: dict[str, Any]) -> str:
    cleaned = {key: value for key, value in params.items() if value is not None}
    if not cleaned:
        return url
    return f"{url}?{urlencode(cleaned)}"


def _health(base_url: str) -> dict[str, Any]:
    payload, _headers = _request("GET", f"{base_url}/health")
    if not isinstance(payload, dict):
        raise AllaRequestError("Alla /health returned a non-JSON response")
    return payload


def _resolve_launch(
    base_url: str,
    launch_name: str,
    project_id: int | None,
) -> int:
    url = _with_query(
        f"{base_url}/api/v1/launch/resolve",
        {"name": launch_name, "project_id": project_id},
    )
    payload, _headers = _request("GET", url)
    if not isinstance(payload, dict) or "launch_id" not in payload:
        raise AllaRequestError("Launch resolve response did not contain launch_id", url=url)
    return int(payload["launch_id"])


def _run_analysis(base_url: str, launch_id: int) -> dict[str, Any]:
    url = _with_query(
        f"{base_url}/api/v1/analyze/{launch_id}",
        {"push_to_testops": "false"},
    )
    payload, _headers = _request("POST", url)
    if not isinstance(payload, dict):
        raise AllaRequestError("Analysis endpoint returned a non-JSON response", url=url)
    return payload


def _write_html_report(base_url: str, launch_id: int) -> dict[str, Any]:
    url = _with_query(
        f"{base_url}/api/v1/analyze/{launch_id}/html",
        {"push_to_testops": "false"},
    )
    body, headers = _request("POST", url, expect_json=False)
    if not isinstance(body, str):
        body = str(body)
    digest = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:12]
    path = Path(tempfile.gettempdir()) / f"alla_launch_{launch_id}_{digest}.html"
    path.write_text(body, encoding="utf-8")
    title = ""
    title_start = body.lower().find("<title>")
    title_end = body.lower().find("</title>")
    if title_start >= 0 and title_end > title_start:
        title = html.unescape(body[title_start + len("<title>") : title_end]).strip()
    return {
        "html_path": str(path),
        "report_url": headers.get("X-Report-URL") or headers.get("x-report-url") or "",
        "html_title": title,
        "html_bytes": len(body.encode("utf-8", errors="replace")),
    }


def _active_failure_count(triage: dict[str, Any]) -> int:
    failed = int(triage.get("failed_count") or 0)
    broken = int(triage.get("broken_count") or 0)
    muted = int(triage.get("muted_failure_count") or 0)
    return max(failed + broken - muted, 0)


def _index_failed_tests(triage: dict[str, Any]) -> dict[int, dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    for test in triage.get("failed_tests") or []:
        if not isinstance(test, dict):
            continue
        test_id = test.get("test_result_id")
        if test_id is None:
            continue
        indexed[int(test_id)] = test
    return indexed


def _entry_category(entry: dict[str, Any]) -> str:
    category = entry.get("category")
    if isinstance(category, dict):
        return str(category.get("value") or category.get("name") or "")
    return str(category or "")


def _compact_kb_match(match: dict[str, Any]) -> dict[str, Any]:
    entry = match.get("entry") if isinstance(match.get("entry"), dict) else {}
    return {
        "title": entry.get("title") or "",
        "category": _entry_category(entry),
        "score": round(float(match.get("score") or 0), 3),
        "matched_on": match.get("matched_on") or [],
        "origin": match.get("match_origin") or "",
        "feedback_vote": match.get("feedback_vote"),
    }


def _cluster_llm(
    cluster_id: str,
    llm_result: dict[str, Any] | None,
) -> dict[str, str]:
    if not isinstance(llm_result, dict):
        return {"llm_verdict": "", "llm_error": ""}
    analyses = llm_result.get("cluster_analyses") or {}
    analysis = analyses.get(cluster_id) if isinstance(analyses, dict) else None
    if not isinstance(analysis, dict):
        return {"llm_verdict": "", "llm_error": ""}
    return {
        "llm_verdict": _truncate(analysis.get("analysis_text")),
        "llm_error": _truncate(analysis.get("error"), 500),
    }


def _representative_test_context(
    cluster: dict[str, Any],
    tests_by_id: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    test_id = cluster.get("representative_test_id")
    if test_id is None:
        member_ids = cluster.get("member_test_ids") or []
        test_id = member_ids[0] if member_ids else None
    if test_id is None:
        return {}
    test = tests_by_id.get(int(test_id), {})
    return {
        "test_result_id": test.get("test_result_id") or test_id,
        "name": test.get("name") or "",
        "full_name": test.get("full_name") or "",
        "status": test.get("status") or "",
        "link": test.get("link") or "",
        "status_message": _truncate(test.get("status_message"), 500),
        "correlation_hint": _truncate(test.get("correlation_hint"), 500),
        "log_snippet": _truncate(test.get("log_snippet"), 700),
    }


def _compact_analysis(
    raw: dict[str, Any],
    *,
    base_url: str,
    health: dict[str, Any],
    html_report: dict[str, Any] | None,
) -> dict[str, Any]:
    triage = raw.get("triage_report") if isinstance(raw.get("triage_report"), dict) else {}
    clustering = (
        raw.get("clustering_report")
        if isinstance(raw.get("clustering_report"), dict)
        else {}
    )
    kb_matches = raw.get("kb_matches") if isinstance(raw.get("kb_matches"), dict) else {}
    llm_result = raw.get("llm_result") if isinstance(raw.get("llm_result"), dict) else None
    tests_by_id = _index_failed_tests(triage)

    compact_clusters: list[dict[str, Any]] = []
    for cluster in clustering.get("clusters") or []:
        if not isinstance(cluster, dict):
            continue
        cluster_id = str(cluster.get("cluster_id") or "")
        signature = cluster.get("signature") if isinstance(cluster.get("signature"), dict) else {}
        matches = kb_matches.get(cluster_id) if isinstance(kb_matches, dict) else []
        if not isinstance(matches, list):
            matches = []
        representative_context = _representative_test_context(cluster, tests_by_id)
        compact_clusters.append(
            {
                "id": cluster_id,
                "label": cluster.get("label") or "",
                "size": int(cluster.get("member_count") or 0),
                "member_test_ids": cluster.get("member_test_ids") or [],
                "representative_test_id": cluster.get("representative_test_id"),
                "representative_message": _truncate(
                    cluster.get("example_message")
                    or signature.get("representative_message")
                    or signature.get("message_pattern")
                ),
                "message_pattern": _truncate(signature.get("message_pattern"), 500),
                "exception_type": signature.get("exception_type") or "",
                "category": signature.get("category") or "",
                "step_path": cluster.get("example_step_path") or "",
                "correlation_hint": cluster.get("example_correlation")
                or representative_context.get("correlation_hint")
                or "",
                "trace_snippet": _truncate(cluster.get("example_trace_snippet"), 700),
                "representative_test": representative_context,
                "kb_matches": [
                    _compact_kb_match(match)
                    for match in matches[:MAX_MATCHES_PER_CLUSTER]
                    if isinstance(match, dict)
                ],
                **_cluster_llm(cluster_id, llm_result),
            }
        )

    summary = raw.get("llm_launch_summary")
    launch_summary = {}
    if isinstance(summary, dict):
        launch_summary = {
            "summary_text": _truncate(summary.get("summary_text"), 1500),
            "error": _truncate(summary.get("error"), 500),
        }

    payload: dict[str, Any] = {
        "ok": True,
        "server": {
            "url": base_url,
            "health": health,
        },
        "launch": {
            "id": triage.get("launch_id"),
            "name": triage.get("launch_name"),
            "project_id": triage.get("project_id"),
        },
        "counters": {
            "total_results": int(triage.get("total_results") or 0),
            "passed": int(triage.get("passed_count") or 0),
            "failed": int(triage.get("failed_count") or 0),
            "broken": int(triage.get("broken_count") or 0),
            "skipped": int(triage.get("skipped_count") or 0),
            "unknown": int(triage.get("unknown_count") or 0),
            "muted_failures": int(triage.get("muted_failure_count") or 0),
            "active_failures": _active_failure_count(triage),
        },
        "clustering": {
            "cluster_count": int(clustering.get("cluster_count") or 0),
            "total_failures": int(clustering.get("total_failures") or 0),
            "unclustered_count": int(clustering.get("unclustered_count") or 0),
            "clusters": sorted(
                compact_clusters,
                key=lambda item: (-int(item.get("size") or 0), item.get("label") or ""),
            ),
        },
        "llm": {
            "total_clusters": int((llm_result or {}).get("total_clusters") or 0),
            "analyzed_count": int((llm_result or {}).get("analyzed_count") or 0),
            "failed_count": int((llm_result or {}).get("failed_count") or 0),
            "skipped_count": int((llm_result or {}).get("skipped_count") or 0),
            "launch_summary": launch_summary,
        },
    }
    if html_report:
        payload["html_report"] = html_report
    return payload


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run read-only Alla analysis through alla-server REST API."
    )
    launch = parser.add_mutually_exclusive_group(required=True)
    launch.add_argument("--launch-id", type=int, help="Allure TestOps launch id.")
    launch.add_argument("--launch-name", help="Exact Allure TestOps launch name.")
    parser.add_argument(
        "--project-id",
        type=int,
        help="Project id used with --launch-name resolution.",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Also generate HTML through /api/v1/analyze/{launch_id}/html.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        base_url = _ensure_configured()
        started_at = time.time()
        health = _health(base_url)
        launch_id = args.launch_id
        if launch_id is None:
            launch_id = _resolve_launch(base_url, args.launch_name, args.project_id)
        raw_analysis = _run_analysis(base_url, launch_id)
        html_report = _write_html_report(base_url, launch_id) if args.html else None
        payload = _compact_analysis(
            raw_analysis,
            base_url=base_url,
            health=health,
            html_report=html_report,
        )
        payload["duration_seconds"] = round(time.time() - started_at, 3)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except AllaRequestError as exc:
        print(json.dumps(exc.to_payload(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

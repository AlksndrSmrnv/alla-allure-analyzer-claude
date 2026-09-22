"""File-based preparation, resumable context reads, and validated reports."""

import json
import re
import subprocess
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .analysis import ClusterAnalysis, LaunchAnalysis
from .evidence import compact_trace, redact, write_json, private_write, secure_artifacts
from .services.clustering_service import ClusteringService
from .services.log_extraction_service import LogExtractionService
from .services.triage_service import TriageService


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def local_path(root: Path, relative: str):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Путь выходит за пределы проекта или снимка")
    return path


def revision(root):
    def git(*args):
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else None

    try:
        head = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=no")
        return {"commit": head, "tracked_changes": bool(dirty), "alignment": "неизвестно"}
    except (OSError, subprocess.TimeoutExpired):
        return {"commit": None, "tracked_changes": None, "alignment": "неизвестно"}


async def prepare(launch_id, project_root, client):
    if launch_id <= 0:
        raise ValueError("Launch ID должен быть положительным числом")
    project_root = Path(project_root).resolve()
    run_id = uuid4().hex
    run_dir = (
        project_root
        / "reports"
        / "alla"
        / str(launch_id)
        / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + run_id[:8])
    )
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    private_write(run_dir / ".gitignore", "*\n")
    for directory in ("tests", "attachments", "analyses"):
        (run_dir / directory).mkdir(mode=0o700)
    client.artifacts = run_dir / "attachments"
    started = now()
    try:
        report = await TriageService(client, client.settings).analyze_launch(launch_id)
        logs = LogExtractionService(client)
        await logs.enrich_with_logs(report.failed_tests)
        for test in report.failed_tests:
            data = redact(test.model_dump(mode="json"), client.secrets)
            write_json(run_dir / "tests" / f"{test.test_result_id}.json", data)
        for test in report.failed_tests:
            for field in ("status_message", "status_trace", "log_snippet"):
                client.sources[f"test:{test.test_result_id}:{field}"] = {
                    "state": "received" if getattr(test, field) else "absent",
                    "test_result_ids": [test.test_result_id],
                }
        # Only already-redacted evidence participates in clustering.
        clustering_config = client.settings.clustering_config()
        clusters = ClusteringService(clustering_config).cluster_failures(
            launch_id, report.failed_tests
        )
        counters = {
            "total_results": report.total_results,
            "passed": report.passed_count,
            "failed": report.failed_count,
            "broken": report.broken_count,
            "skipped": report.skipped_count,
            "unknown": report.unknown_count,
            "muted_failures": report.muted_failure_count,
            "active_failures": report.active_failure_count,
            "hidden_results": client.hidden_count,
        }
        limitations = [
            f"{key}: {value['state']}"
            for key, value in client.sources.items()
            if value["state"] in ("unavailable", "truncated", "skipped")
        ]
        limitations.extend(logs.issues)
        for test in report.failed_tests:
            if not (test.status_message or test.status_trace or test.log_snippet):
                limitations.append(
                    f"test:{test.test_result_id}: диагностические данные отсутствуют"
                )
        if not client.launch.closed:
            limitations.insert(0, "Прогон открыт: результаты могли изменяться во время сбора")
        data = {
            "schema_version": 1,
            "analysis_config": {
                "clustering": asdict(clustering_config),
                "max_detail_enrichments": client.settings.max_detail_enrichments,
            },
            "run_id": run_id,
            "project_root": str(project_root),
            "started_at": started,
            "finished_at": now(),
            "launch": client.launch.model_dump(mode="json"),
            "launch_url": client.settings.endpoint.rstrip("/") + f"/launch/{launch_id}",
            "project_revision": revision(project_root),
            "counters": counters,
            "sources": client.sources,
            "limitations": limitations,
            "active_test_ids": sorted(t.test_result_id for t in report.failed_tests),
            "clusters": clusters.model_dump(mode="json")["clusters"],
        }
        check_coverage(data)
        write_json(run_dir / "run.json", redact(data, client.secrets))
        return run_dir
    except Exception as exc:
        write_json(
            run_dir / "preparation-error.json",
            {
                "state": "incomplete",
                "started_at": started,
                "finished_at": now(),
                "reason": redact(str(exc), client.secrets),
                "sources": client.sources,
            },
        )
        raise


def check_coverage(data):
    members = [tid for c in data["clusters"] for tid in c["member_test_ids"]]
    ids = [c["cluster_id"] for c in data["clusters"]]
    if (
        len(ids) != len(set(ids))
        or len(members) != len(set(members))
        or sorted(members) != sorted(data["active_test_ids"])
    ):
        raise ValueError("Нарушено покрытие падений кластерами")
    if len(members) != data["counters"]["active_failures"]:
        raise ValueError("Счётчик активных падений не совпадает с составом")
    if any(c["member_count"] != len(c["member_test_ids"]) for c in data["clusters"]):
        raise ValueError("Неверный размер кластера")


def cluster_for(data, cluster_id):
    for cluster in data["clusters"]:
        if cluster["cluster_id"] == cluster_id:
            return cluster
    raise ValueError("Неизвестный ID кластера")


def clip(text, limit):
    text = text or ""
    return (
        text
        if len(text) <= limit
        else text[:limit] + f"\n[обрезано; полный локальный текст: {len(text)} символов]"
    )


def context(run_dir, cluster_id=None, *, member_id=None, source=None, offset=0, limit=8000):
    run_dir = Path(run_dir).resolve()
    data = read_json(run_dir / "run.json")
    check_coverage(data)
    secure_artifacts(run_dir)
    if source is not None:
        if offset < 0 or not 1 <= limit <= 16000:
            raise ValueError("offset >= 0; limit от 1 до 16000")
        text = source_text(data, run_dir, source)
        return {
            "source": source,
            "offset": offset,
            "text": text[offset : offset + limit],
            "total_chars": len(text),
            "next_offset": offset + limit if offset + limit < len(text) else None,
            "availability": data["sources"].get(source),
        }
    if cluster_id is None:
        pending, completed, invalid = [], [], []
        for c in data["clusters"]:
            cid = c["cluster_id"]
            path = run_dir / "analyses" / f"{cid}.json"
            try:
                validated_analysis(data, run_dir, cid)
                completed.append(cid)
            except (ValueError, OSError, KeyError):
                pending.append(cid)
                if path.exists():
                    invalid.append(cid)
        return {
            "run_id": data["run_id"],
            "launch": data["launch"],
            "counters": data["counters"],
            "project_revision": data["project_revision"],
            "pending": pending,
            "completed": completed,
            "invalid": invalid,
            "limitations": data["limitations"],
            "run_dir": str(run_dir),
        }
    cluster = cluster_for(data, cluster_id)
    if member_id is not None and member_id not in cluster["member_test_ids"]:
        raise ValueError("Тест не входит в кластер")
    examples = []
    for tid in [member_id] if member_id is not None else cluster["example_test_ids"]:
        test = read_json(run_dir / "tests" / f"{tid}.json")
        hints = [part for part in re.split(r"[./:#]", test.get("full_name") or "") if len(part) > 3]
        examples.append(
            {
                **{
                    k: test.get(k)
                    for k in (
                        "test_result_id",
                        "name",
                        "full_name",
                        "status",
                        "link",
                        "failed_step_path",
                        "correlation_hint",
                    )
                },
                "status_message": clip(test.get("status_message"), 4000),
                "status_trace": clip(compact_trace(test.get("status_trace"), hints), 8000),
                "log_snippet": clip(test.get("log_snippet"), 8000),
                "evidence_sources": [
                    f"test:{tid}:{k}" for k in ("status_message", "status_trace", "log_snippet")
                ],
            }
        )
    members = [read_json(run_dir / "tests" / f"{tid}.json") for tid in cluster["member_test_ids"]]
    variants = Counter(
        (clip(t.get("status_message"), 300), t.get("failed_step_path")) for t in members
    )
    return {
        "run_id": data["run_id"],
        "cluster_id": cluster_id,
        "size": cluster["member_count"],
        "member_test_ids": cluster["member_test_ids"],
        "examples": examples,
        "variants": [
            {"message": key[0], "step": key[1], "count": count}
            for key, count in variants.most_common(20)
        ],
        "variant_count": len(variants),
        "sources": {
            k: v
            for k, v in data["sources"].items()
            if k
            in {
                f"{prefix}:{tid}"
                for tid in cluster["member_test_ids"]
                for prefix in ("execution", "detail", "attachments")
            }
            or set(v.get("test_result_ids", [])) & set(cluster["member_test_ids"])
        },
        "project_revision": data["project_revision"],
    }


def source_text(data, run_dir, source):
    match = re.fullmatch(
        r"test:(\d+):(status_message|status_trace|log_snippet|failed_step_path)", source
    )
    if match:
        tid = int(match[1])
        if tid not in data["active_test_ids"]:
            raise ValueError("Неизвестный источник теста")
        return read_json(run_dir / "tests" / f"{tid}.json").get(match[2]) or ""
    if source.startswith("attachment:"):
        info = data["sources"].get(source, {})
        if "file" not in info:
            raise ValueError("Источник не был скачан")
        return local_path(run_dir, info["file"]).read_text(encoding="utf-8")
    match = re.fullmatch(r"code:(.+):(\d+)", source)
    if match:
        path = local_path(Path(data["project_root"]), match[1])
        suffixes = {
            ".py",
            ".java",
            ".kt",
            ".kts",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".cs",
            ".go",
            ".rs",
            ".rb",
            ".php",
            ".scala",
            ".swift",
            ".c",
            ".cpp",
            ".h",
            ".hpp",
            ".feature",
            ".robot",
            ".groovy",
            ".sh",
            ".sql",
        }
        original = Path(match[1])
        forbidden = {".git", ".ssh", ".aws", ".azure", ".qwen", "secrets", "credentials"}
        if (
            original.is_absolute()
            or any(
                candidate.name.lower().startswith(".env")
                or set(s.lower() for s in candidate.suffixes)
                & {".env", ".pem", ".key", ".p12", ".pfx"}
                for candidate in (path, original)
            )
            or any(part.lower() in forbidden for part in (*path.parts, *original.parts))
            or path.suffix.lower() not in suffixes
            or original.suffix.lower() not in suffixes
        ):
            raise ValueError("Секреты и служебные Git-файлы не являются доказательствами")
        contents = path.read_text(encoding="utf-8")
        if re.search(r"-----BEGIN [^-]*(?:PRIVATE KEY|CERTIFICATE)-----", contents):
            raise ValueError("Ключи и сертификаты не являются доказательствами кода")
        lines = contents.splitlines()
        line = int(match[2])
        if not 1 <= line <= len(lines):
            raise ValueError("Строка кода не существует")
        return redact("\n".join(lines[line - 1 : line + 19]))
    raise ValueError("Некорректный источник доказательства")


def validate_evidence(data, run_dir, evidence):
    for item in evidence:
        if item.quote not in source_text(data, run_dir, item.source):
            raise ValueError(f"Цитата не найдена в источнике {item.source}")


def validated_analysis(data, run_dir, cid):
    path = run_dir / "analyses" / f"{cid}.json"
    if not path.exists():
        raise ValueError(f"Отсутствует анализ кластера {cid}")
    analysis = ClusterAnalysis.model_validate(read_json(path))
    if analysis.run_id != data["run_id"] or analysis.cluster_id != cid:
        raise ValueError("Анализ относится к другому снимку или кластеру")
    if analysis.category != "неизвестно" and not analysis.evidence:
        raise ValueError("Для установленной категории нужны доказательства")
    if analysis.category == "неизвестно" and analysis.confidence != "низкая":
        raise ValueError("Неизвестная причина требует низкой уверенности")
    allowed = set(cluster_for(data, cid)["member_test_ids"])
    for item in analysis.evidence:
        if item.source.startswith("test:") and int(item.source.split(":")[1]) not in allowed:
            raise ValueError("Доказательство принадлежит другому кластеру")
        if item.source.startswith("attachment:") and not allowed.intersection(
            data["sources"].get(item.source, {}).get("test_result_ids", [])
        ):
            raise ValueError("Вложение принадлежит другому кластеру")
    validate_evidence(data, run_dir, analysis.evidence)
    return analysis


def evidence_link(data, run_dir, source):
    import os
    from urllib.parse import quote

    if source.startswith("code:"):
        relative, line = source[5:].rsplit(":", 1)
        path = local_path(Path(data["project_root"]), relative)
        target = quote(os.path.relpath(path, run_dir))
        return f"[{relative}:{line}]({target}#L{line})"
    if source.startswith("attachment:"):
        return f"[{source}]({data['sources'][source]['file']})"
    if source.startswith("test:"):
        return f"[{source}](tests/{source.split(':')[1]}.json)"
    return source


def finalize(run_dir):
    run_dir = Path(run_dir).resolve()
    data = read_json(run_dir / "run.json")
    check_coverage(data)
    secure_artifacts(run_dir)
    analyses = {
        c["cluster_id"]: validated_analysis(data, run_dir, c["cluster_id"])
        for c in data["clusters"]
    }
    if {p.stem for p in (run_dir / "analyses").glob("*.json")} != set(analyses):
        raise ValueError("Обнаружены лишние или повторные файлы анализа")
    if not (run_dir / "summary.json").exists():
        raise ValueError("Отсутствует общий анализ прогона")
    summary = LaunchAnalysis.model_validate(read_json(run_dir / "summary.json"))
    if summary.run_id != data["run_id"]:
        raise ValueError("Общий анализ относится к другому снимку")
    for finding in summary.findings:
        if len(set(finding.cluster_ids)) != len(finding.cluster_ids) or not set(
            finding.cluster_ids
        ) <= set(analyses):
            raise ValueError("Неверные ID кластеров в общем анализе")
        validate_evidence(data, run_dir, finding.evidence)
    c = data["counters"]
    text = "\n".join(summary.summary_lines)
    lines = [
        f"# Разбор прогона {data['launch']['id']}",
        "",
        f"[Открыть TestOps]({data['launch_url']})",
        "",
        text,
        "",
        f"Всего: {c['total_results']}; passed: {c['passed']}; failed: {c['failed']}; broken: {c['broken']}; skipped: {c['skipped']}; unknown: {c['unknown']}.",
        f"Активных падений: {c['active_failures']}; muted: {c['muted_failures']}; скрытых попыток: {c['hidden_results']}; кластеров: {len(analyses)}.",
        f"Сбор данных: {data['started_at']} — {data['finished_at']}.",
        "",
        "## Ограничения данных",
        "",
    ]
    lines.extend("- " + item for item in data["limitations"])
    lines.append(
        "- Соответствие версии кода прогону проверяется отдельно в каждом кластере; отсутствие сведений о версии не означает совпадение."
    )
    lines.extend(
        ["", "## Приоритетные действия", ""] + ["- " + item for item in summary.priority_actions]
    )
    if summary.findings:
        lines.extend(["", "## Связи между кластерами", ""])
        for finding in summary.findings:
            ids = set(finding.cluster_ids)
            count = len(
                {
                    tid
                    for cluster in data["clusters"]
                    if cluster["cluster_id"] in ids
                    for tid in cluster["member_test_ids"]
                }
            )
            lines.append(
                f"- {finding.text} ({finding.status}; {count} тестов; кластеры: {', '.join(finding.cluster_ids)})."
            )
            lines.extend(
                f"  - {evidence_link(data, run_dir, e.source)}: {e.quote}" for e in finding.evidence
            )
    lines.extend(["", "## Кластеры", ""])
    for cluster in data["clusters"]:
        a = analyses[cluster["cluster_id"]]
        test = read_json(run_dir / "tests" / f"{cluster['representative_test_id']}.json")
        lines.extend(
            [
                f"### {a.cluster_id} — {cluster['member_count']} тестов",
                "",
                f"{a.symptom} {a.cause} Категория: {a.category}; уверенность: {a.confidence}. Следующий шаг: {a.next_action}",
                f"Код: {a.code_alignment} — {a.code_alignment_reason}.",
            ]
        )
        for e in a.evidence:
            lines.append(f"- {evidence_link(data, run_dir, e.source)}: {e.quote}")
        for item in a.limitations + a.contradictions:
            lines.append(f"- Ограничение/противоречие: {item}")
        lines.extend([f"[Пример падения]({test['link']})", ""])
    rendered = redact("\n".join(lines)) + "\n"
    output = run_dir / "report.md"
    private_write(output, rendered)
    return {"report": str(output), "summary": redact(text), "analyzed_clusters": len(analyses)}

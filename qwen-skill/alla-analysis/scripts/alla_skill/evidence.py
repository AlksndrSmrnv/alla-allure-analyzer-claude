"""Keep diagnostic values separate from clustering normalization."""

import json
import re
from pathlib import Path
from uuid import uuid4

_SECRET = re.compile(
    r"""(?ix)(["']?(?:password|passwd|secret|token|access_token|refresh_token|(?:x[_-])?api[_-]?key|client_secret)["']?\s*[:=]\s*)("[^"\n]*(?:"|$)|'[^'\n]*(?:'|$)|[^\s,;&}]+)"""
)
_AUTH = re.compile(r'(?im)(authorization\s*["\']?\s*[:=]\s*["\']?)(?:bearer|basic)\s+[^\s"\',;]+')
_COOKIE = re.compile(r"(?im)((?:set-cookie|cookie)\s*[:=]\s*)[^\r\n]+")


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {
            k: "[REDACTED]"
            if re.fullmatch(
                r"(?i)(password|passwd|secret|.*token|(?:x[_-])?api[_-]?key|client_secret|authorization|(?:set-)?cookie)",
                k,
            )
            else redact(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)",
        "[REDACTED PRIVATE KEY]",
        value,
        flags=re.S,
    )
    value = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", value)
    value = _AUTH.sub(r"\1[REDACTED]", value)
    value = _COOKIE.sub(r"\1[REDACTED]", value)

    def replace_secret(match):
        quote = match[2][0] if match[2][0] in ('"', "'") else ""
        return match[1] + quote + "[REDACTED]" + quote

    return _SECRET.sub(replace_secret, value)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def compact_trace(text: str | None, project_hints=(), max_lines=60):
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return text or ""
    priorities = list(range(min(8, len(lines))))
    for i, line in enumerate(lines):
        if re.search(
            r"Caused by:|Suppressed:|During handling|direct cause|^\w*(?:Error|Exception)\b", line
        ) or any(h and h in line for h in project_hints):
            priorities.extend(range(max(0, i - 1), min(len(lines), i + 3)))
    priorities.extend(range(max(0, len(lines) - 8), len(lines)))
    selected = sorted(dict.fromkeys(priorities[:]))
    # Preserve cause lines first when the trace contains many application frames.
    if len(selected) > max_lines:
        causes = [
            i
            for i in selected
            if re.search(r"Caused by:|Suppressed:|During handling|direct cause", lines[i])
        ]
        selected = sorted(list(dict.fromkeys(causes + priorities))[:max_lines])
    out, previous = [], -1
    for i in selected:
        if i > previous + 1:
            out.append(f"[пропущены строки {previous + 2}–{i}]")
        out.append(f"{i + 1}: {lines[i]}")
        previous = i
    if previous < len(lines) - 1:
        out.append(f"[пропущены строки {previous + 2}–{len(lines)}]")
    return "\n".join(out)

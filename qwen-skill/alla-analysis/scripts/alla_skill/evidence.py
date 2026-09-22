"""Keep diagnostic values separate from clustering normalization."""

import json
import os
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


def decode_attachment(content: bytes, *, declared_text: bool = False) -> str | None:
    """Conservative text detection, not a libmagic replacement."""
    signatures = (
        b"\x89PNG",
        b"\xff\xd8\xff",
        b"GIF87a",
        b"GIF89a",
        b"%PDF",
        b"PK\x03\x04",
        b"\x7fELF",
        b"\x1f\x8b",
        b"Rar!",
        b"7z\xbc\xaf",
        b"BZh",
        b"RIFF",
        b"SQLite format",
    )
    if content.startswith(signatures):
        return None
    try:
        if content.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = content.decode("utf-16")
        else:
            text = content.decode("utf-8-sig")
    except UnicodeError:
        if not declared_text:
            return None
        from charset_normalizer import from_bytes

        best = from_bytes(content).best()
        if best is None or best.chaos > 0.05:
            return None
        text = str(best)
    sample = text[:8192]
    if any(ord(c) < 32 and c not in "\n\r\t" for c in sample):
        return None
    if sample and sum(c.isprintable() or c in "\n\r\t" for c in sample) / len(sample) < 0.98:
        return None
    return text


def private_write(path: Path, text: str):
    """Create a 0600 temporary file, then replace atomically (POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path: Path, value):
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def secure_artifacts(run_dir: Path):
    """Normalize agent-written artifact permissions; reject links before chmod."""
    paths = [run_dir, *run_dir.rglob("*")]
    links = [str(path.relative_to(run_dir)) for path in paths if path.is_symlink()]
    if links:
        raise ValueError(
            "Символические ссылки внутри снимка не поддерживаются: "
            + ", ".join(sorted(links))
            + ". Удалите указанные ссылки или замените обычными файлами; не удаляйте их цели."
        )
    for path in paths:
        path.chmod(0o700 if path.is_dir() else 0o600)


def redact_configuration(contents: str) -> str:
    """Mask credential assignments before slicing; fail closed for multiline values."""
    lines = contents.splitlines()
    secret_key = re.compile(
        r"(?i)^\s*(?:-\s*)?[\"']?[\w.-]*(?:password|passwd|secret|token|api[_-]?key|credential|authorization|cookie)[\w.-]*[\"']?\s*[:=]\s*(.*)$"
    )
    for index, line in enumerate(lines):
        match = secret_key.match(line)
        if not match:
            continue
        value = match[1].strip()
        indent = len(line) - len(line.lstrip())
        following = lines[index + 1 :]
        next_line = next((v for v in following if v.strip() and not v.lstrip().startswith("#")), "")
        if (
            not value
            or value.startswith(("|", ">", '"""', "'''", "{", "[", "&"))
            or value.endswith("\\")
            or (value.startswith(('"', "'")) and value[0] not in value[1:])
            or (next_line and len(next_line) - len(next_line.lstrip()) > indent)
        ):
            raise ValueError(
                "Конфиг содержит многострочное или структурное секретное значение; "
                "используйте другой источник доказательства"
            )
        lines[index] = line[: match.start(1)] + "[REDACTED]"
    return redact("\n".join(lines))


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

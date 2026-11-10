"""Local, immutable release staging; stdlib only, compatible with Python 3.8."""
from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from typing import IO

MAX_ARCHIVE = 100 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_UNPACKED = 256 * 1024 * 1024
EXCLUDED = {".venv", ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".alla-releases", ".alla-version", ".alla-setup-complete"}


def excluded(name: str) -> bool:
    for part in PurePosixPath(name).parts:
        if part in EXCLUDED or part.endswith((".pyc", ".pyo")):
            return True
        if part != ".env.example" and (part == ".env" or part.startswith((".env.", ".env~", ".env-", ".env_"))):
            return True
    return False


def safe_name(raw: str) -> str:
    path = PurePosixPath(raw)
    if (not raw or "\\" in raw or raw.startswith("/") or any(p in ("", ".", "..") for p in raw.rstrip("/").split("/"))
            or ":" in path.parts[0] or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
        raise ValueError("Небезопасный путь в релизе: {!r}".format(raw))
    return path.as_posix().rstrip("/")


class Collector:
    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.seen: dict[str, str] = {}
        self.namespace: dict[str, str] = {}
        self.count = 0
        self.size = 0
        self.skipped: list[str] = []

    def add(self, raw: str, directory: bool, source: IO[bytes] | None = None) -> None:
        name = safe_name(raw)
        self.count += 1
        if self.count > MAX_ENTRIES:
            raise ValueError("В релизе больше 10000 записей")
        folded = name.casefold()
        if folded in self.seen:
            raise ValueError("Повторяющиеся пути или различие только регистром: " + name)
        self.seen[folded] = name
        for component in [PurePosixPath(name), *PurePosixPath(name).parents]:
            if component == PurePosixPath("."):
                continue
            normalized = component.as_posix()
            previous = self.namespace.setdefault(normalized.casefold(), normalized)
            if previous != normalized:
                raise ValueError("Коллизия регистра пути: " + name)
        # Implicit parent names must have the same case and cannot be files.
        for parent in PurePosixPath(name).parents:
            if parent == PurePosixPath("."):
                continue
            parent_previous = self.seen.get(parent.as_posix().casefold())
            if parent_previous is not None and parent_previous != parent.as_posix():
                raise ValueError("Коллизия регистра пути: " + name)
        if excluded(name):
            self.skipped.append(name)
            # Count actual decompressed bytes even for excluded entries.
            if source is not None:
                self.copy(source, None)
            return
        target = self.destination / name
        if directory:
            target.mkdir(parents=True, exist_ok=True)
        else:
            if source is None:
                raise ValueError("В релизе нет содержимого файла: " + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                self.copy(source, output)

    def copy(self, source: IO[bytes], output: IO[bytes] | None) -> None:
        while True:
            chunk = source.read(min(1024 * 1024, MAX_UNPACKED - self.size + 1))
            if not chunk:
                break
            self.size += len(chunk)
            if self.size > MAX_UNPACKED:
                raise ValueError("Распакованный релиз больше 256 MiB")
            if output is not None:
                output.write(chunk)


def collect(source: Path, destination: Path) -> list[str]:
    try:
        return _collect(source, destination)
    except (tarfile.TarError, zipfile.BadZipFile, RuntimeError, EOFError) as error:
        raise ValueError("Повреждённый или зашифрованный архив релиза") from error


def _secure_flags(*, directory: bool = False) -> int:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ValueError("Безопасное чтение релиза требует Linux или macOS")
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0)


def _checked_open(name: str, parent: int, expected: os.stat_result) -> int:
    """Open exactly the stat'ed entry, without following links or blocking on FIFO."""
    if not (stat.S_ISDIR(expected.st_mode) or stat.S_ISREG(expected.st_mode)):
        raise ValueError("Ссылки и специальные файлы запрещены; источник должен быть обычным файлом или каталогом: " + name)
    if stat.S_ISREG(expected.st_mode) and expected.st_nlink != 1:
        raise ValueError("Ссылки и специальные файлы запрещены: " + name)
    try:
        descriptor = os.open(name, _secure_flags(directory=stat.S_ISDIR(expected.st_mode)),
                             dir_fd=parent)
    except OSError as error:
        raise ValueError("Источник релиза изменился или содержит ссылку: " + name) from error
    try:
        actual = os.fstat(descriptor)
        if ((actual.st_dev, actual.st_ino, stat.S_IFMT(actual.st_mode)) !=
                (expected.st_dev, expected.st_ino, stat.S_IFMT(expected.st_mode)) or
                (stat.S_ISREG(actual.st_mode) and actual.st_nlink != 1)):
            raise ValueError("Источник релиза изменился во время чтения: " + name)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextlib.contextmanager
def _source_descriptor(source: Path) -> Iterator[int]:
    # Only root-owned system aliases are trusted. User-writable symlink parents
    # must not be resolved before pinning: that would move the source boundary.
    absolute = Path(os.path.abspath(str(source)))
    components = list(absolute.parent.parts[1:])
    current = os.open(absolute.anchor, _secure_flags(directory=True))
    try:
        aliases = {"tmp": "private/tmp", "var": "private/var", "etc": "private/etc"}
        if components and components[0] in aliases:
            first = components[0]
            metadata = os.stat(first, dir_fd=current, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                target = aliases[first]
                if metadata.st_uid != 0 or os.readlink(first, dir_fd=current) != target:
                    raise ValueError("Недоверенная ссылка в пути источника релиза")
                components = target.split("/") + components[1:]
        for component in components:
            try:
                next_descriptor = os.open(component, _secure_flags(directory=True), dir_fd=current)
            except OSError as error:
                raise ValueError("Родитель источника релиза изменился или содержит ссылку") from error
            os.close(current)
            current = next_descriptor
        name = absolute.name or "."
        expected = os.stat(name, dir_fd=current, follow_symlinks=False)
        descriptor = _checked_open(name, current, expected)
        try:
            yield descriptor
        finally:
            os.close(descriptor)
    finally:
        os.close(current)


def _directory(descriptor: int, prefix: str, collector: Collector) -> None:
    for name in sorted(os.listdir(descriptor)):
        relative = str(PurePosixPath(prefix) / name)
        expected = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(expected.st_mode):
            collector.add(relative, True)
            if excluded(relative):
                continue
            child = _checked_open(name, descriptor, expected)
            try:
                _directory(child, relative, collector)
            finally:
                os.close(child)
        elif stat.S_ISREG(expected.st_mode) and expected.st_nlink == 1:
            if excluded(relative):
                collector.add(relative, False)  # Never open .env or excluded cache payload.
                continue
            child = _checked_open(name, descriptor, expected)
            with os.fdopen(child, "rb") as incoming:
                collector.add(relative, False, incoming)
        else:
            raise ValueError("Ссылки и специальные файлы запрещены: " + relative)


@contextlib.contextmanager
def _archive_snapshot(descriptor: int) -> Iterator[IO[bytes]]:
    """Bound a single opened archive's actual bytes before format probing."""
    before = os.fstat(descriptor)
    if before.st_size > MAX_ARCHIVE:
        raise ValueError("Архив больше 100 MiB")
    with os.fdopen(os.dup(descriptor), "rb") as incoming, tempfile.TemporaryFile() as archive:
        size = 0
        while True:
            chunk = incoming.read(min(1024 * 1024, MAX_ARCHIVE - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ARCHIVE:
                raise ValueError("Архив больше 100 MiB")
            archive.write(chunk)
        after = os.fstat(descriptor)
        if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("Архив изменился во время чтения")
        archive.seek(0)
        yield archive


def _archive(archive: IO[bytes], collector: Collector) -> None:
    if zipfile.is_zipfile(archive):
        archive.seek(0)
        with zipfile.ZipFile(archive) as zip_archive:
            for entry in zip_archive.infolist():
                mode = entry.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("Ссылки и специальные файлы запрещены: " + entry.filename)
                if entry.is_dir():
                    collector.add(entry.filename, True)
                else:
                    with zip_archive.open(entry) as zip_stream:
                        collector.add(entry.filename, False, zip_stream)
    else:
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode="r:*") as tar_archive:
            for member in tar_archive:
                if member.isdir():
                    collector.add(member.name, True)
                elif member.isfile():
                    tar_stream = tar_archive.extractfile(member)
                    if tar_stream is None:
                        raise ValueError("Нет содержимого файла: " + member.name)
                    with tar_stream:
                        collector.add(member.name, False, tar_stream)
                else:
                    raise ValueError("Ссылки и специальные файлы запрещены: " + member.name)


def _collect(source: Path, destination: Path) -> list[str]:
    collector = Collector(destination)
    with _source_descriptor(source) as descriptor:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            _directory(descriptor, "", collector)
        else:
            with _archive_snapshot(descriptor) as archive:
                _archive(archive, collector)
    return collector.skipped


def skill_root(root: Path) -> Path:
    candidates = [root, root / "qwen-skill" / "alla-launch"]
    candidates.extend(p for p in root.iterdir() if p.is_dir())
    found = []
    for candidate in candidates:
        marker = candidate / "SKILL.md"
        if marker.is_file() and re.search(r"^name:\s*alla-launch\s*$", marker.read_text(encoding="utf-8")[:2000], re.M):
            if not all((candidate / path).is_file() for path in ("scripts/alla_skill.py", "requirements.txt")):
                raise ValueError("Неполный релиз: нужны scripts/alla_skill.py и requirements.txt")
            found.append(candidate)
    unique = set(found)
    if len(unique) != 1:
        raise ValueError("Нужен единственный корень скилла alla-launch (SKILL.md, scripts/alla_skill.py, requirements.txt)")
    return unique.pop()


def snapshot(root: Path, *, strict: bool = False) -> dict[str, str]:
    result = {}
    for folder, dirs, files in os.walk(str(root), followlinks=False):
        for directory in dirs:
            if strict and excluded(directory):
                raise ValueError("В подготовленный релиз добавлен исключённый каталог: " + directory)
            if not excluded(directory) and (Path(folder) / directory).is_symlink():
                raise ValueError("В релизе обнаружена ссылка: " + directory)
        dirs[:] = sorted(d for d in dirs if not excluded(d))
        for name in sorted(files):
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if excluded(relative):
                if strict:
                    raise ValueError("В подготовленный релиз добавлен исключённый файл: " + relative)
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError("В релизе обнаружена ссылка: " + relative)
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(result.items()))


def digest_tree(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def diff_text(old: Path, new: Path, after: dict[str, str]) -> str:
    before = snapshot(old)
    output: list[str] = []
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) == after.get(name):
            continue
        left = (old / name).read_bytes() if name in before else b""
        right = (new / name).read_bytes() if name in after else b""
        try:
            if b"\x00" in left + right:
                raise UnicodeDecodeError("utf8", b"", 0, 1, "binary")
            output.extend(difflib.unified_diff(left.decode("utf-8").splitlines(True), right.decode("utf-8").splitlines(True), fromfile="active/" + name, tofile="release/" + name))
        except UnicodeDecodeError:
            output.append("Binary {}: {} bytes sha256={} -> {} bytes sha256={}\n".format(name, len(left), before.get(name, "absent"), len(right), after.get(name, "absent")))
    return "".join(output) or "Нет изменений полезной нагрузки.\n"


def source_argument(argv: Sequence[str], project: Path) -> Path:
    args = list(argv)
    source = ""
    if len(args) == 2 and args[0] == "--source":
        source = args[1]
    elif len(args) == 1 and args[0].startswith("--source="):
        source = args[0].split("=", 1)[1]
    elif args:
        raise ValueError("update --source PATH: принимается только локальный источник; --ref удалён")
    config = project / "alla-extensions" / "config.toml"
    values = {}
    if config.exists():
        inside = False
        for line in config.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("["):
                inside = line.strip() == "[update]"
            elif inside:
                match = re.fullmatch(r"\s*(\w+)\s*=\s*['\"]([^'\"]*)['\"]\s*(?:#.*)?", line)
                if match:
                    values[match[1]] = match[2]
        if any(key in values for key in ("repo", "ref", "path")):
            raise ValueError("Старая [update] repo/ref/path больше не поддерживается. Замени её на source = '/local/release.zip' или передай update --source PATH")
    source = source or values.get("source", "")
    if not source or "://" in source or re.match(r"^[^/]+@[^/]+:", source) or source.startswith("-"):
        raise ValueError("Укажи локальный источник: update --source PATH либо [update] source = '/local/release.zip'")
    path = Path(source).expanduser()
    return (path if path.is_absolute() else project / path).absolute()


def stage_release(argv: Sequence[str], project: Path, active: Path) -> tuple[Path, Path]:
    source = source_argument(argv, project)
    releases = project / ".alla-releases"
    if releases.is_symlink():
        raise ValueError(".alla-releases не может быть симлинком")
    releases.mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(releases)))
    try:
        unpacked = temporary / "unpacked"
        unpacked.mkdir()
        skipped = collect(source, unpacked)
        root = skill_root(unpacked)
        files = snapshot(root)
        digest = digest_tree(files)
        release = releases / digest
        diff = diff_text(active, root, files)
        if release.exists() or release.is_symlink():
            if release.is_symlink() or not (release / "complete").is_file() or any((release / item).is_symlink() for item in ("manifest.json", "complete", "alla-launch")):
                raise ValueError("Ранее подготовленный релиз повреждён: " + str(release))
            saved = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
            if saved != files or snapshot(release / "alla-launch", strict=True) != files or (release / "complete").read_text().strip() != digest:
                raise ValueError("Ранее подготовленный релиз повреждён; автоматической подмены нет: " + str(release))
        else:
            output = temporary / "release"
            output.mkdir()
            shutil.copytree(str(root), str(output / "alla-launch"))
            (output / "manifest.json").write_text(json.dumps(files, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            (output / "diff.patch").write_text(diff, encoding="utf-8")
            (output / "excluded.txt").write_text("\n".join(skipped) + "\n", encoding="utf-8")
            (output / "complete").write_text(digest + "\n", encoding="utf-8")
            os.rename(str(output), str(release))
        diff_directory = releases / ".diffs"
        if diff_directory.is_symlink():
            raise ValueError(".alla-releases/.diffs не может быть симлинком")
        diff_directory.mkdir(exist_ok=True)
        diff_path = diff_directory / (digest + "-" + hashlib.sha256(diff.encode("utf-8")).hexdigest() + ".patch")
        if diff_path.is_symlink():
            raise ValueError("Diff не может быть симлинком")
        if not diff_path.exists():
            with diff_path.open("x", encoding="utf-8") as stream:
                stream.write(diff)
        elif diff_path.read_text(encoding="utf-8") != diff:
            raise ValueError("Повреждён сохранённый diff")
        return release / "alla-launch", diff_path
    finally:
        shutil.rmtree(str(temporary))

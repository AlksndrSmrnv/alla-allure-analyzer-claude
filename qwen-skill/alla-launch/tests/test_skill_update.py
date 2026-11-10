"""Local releases are inspectable and never replace an active skill."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import skill_fixtures  # noqa: F401

import alla_release as release


def payload(folder: Path, version: str = "new") -> Path:
    folder.mkdir(parents=True)
    (folder / "scripts").mkdir()
    (folder / "SKILL.md").write_text("---\nname: alla-launch\ndescription: test\n---\n" + version)
    (folder / "scripts/alla_skill.py").write_text("# " + version)
    (folder / "requirements.txt").write_text("locked\n")
    return folder


def stage(tmp_path: Path, source: Path) -> tuple[Path, Path]:
    active = tmp_path / "active"
    if not active.exists():
        payload(active, "old")
        (active / ".env").write_text("TOKEN=secret")
    return release.stage_release(["--source", str(source)], tmp_path, active)


def test_local_directory_stages_diff_and_preserves_active(tmp_path: Path) -> None:
    source = payload(tmp_path / "source")
    (source / ".env").write_text("secret-source")
    (source / ".env.bak").write_text("backup-secret")
    (source / ".env.example").write_text("TOKEN=")
    staged, diff = stage(tmp_path, source)
    assert staged.parent.parent == tmp_path / ".alla-releases"
    assert (staged / "scripts/alla_skill.py").read_text() == "# new"
    assert not (staged / ".env").exists()
    assert not (staged / ".env.bak").exists()
    assert (staged / ".env.example").is_file()
    assert (tmp_path / "active/scripts/alla_skill.py").read_text() == "# old"
    assert (tmp_path / "active/.env").read_text() == "TOKEN=secret"
    assert "secret" not in diff.read_text()
    assert "-# old" in diff.read_text() and "+# new" in diff.read_text()
    assert not (tmp_path / ".qwen").exists()
    assert stage(tmp_path, source) == (staged, diff)
    assert not list((tmp_path / ".alla-releases").glob(".staging-*"))


@pytest.mark.parametrize("kind", ["zip", "tar", "tgz"])
def test_archives_and_single_wrapper_have_same_digest(tmp_path: Path, kind: str) -> None:
    source = payload(tmp_path / "source")
    expected, _ = stage(tmp_path, source)
    archive = tmp_path / ("release." + kind)
    if kind == "zip":
        with zipfile.ZipFile(archive, "w") as output:
            for path in source.rglob("*"):
                if path.is_file():
                    output.write(path, "wrapper/" + path.relative_to(source).as_posix())
    else:
        with tarfile.open(archive, "w:gz" if kind == "tgz" else "w") as output:
            output.add(source, arcname="wrapper")
    actual, _ = stage(tmp_path, archive)
    assert actual == expected


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../escape", "a\\escape", "C:/escape", "./bad", "a\nb", "a\tb", "a\x1fb", "a\x7fb"])
def test_archive_traversal_rejected_before_publish(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, "bad")
    with pytest.raises(ValueError, match="Небезопасный"):
        stage(tmp_path, archive)
    assert not (tmp_path / "escape").exists()
    assert not list((tmp_path / ".alla-releases").iterdir())


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_tar_links_special_files_rejected(tmp_path: Path, kind: str) -> None:
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as output:
        entry = tarfile.TarInfo("bad")
        entry.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE}[kind]
        entry.linkname = "/outside"
        output.addfile(entry)
    with pytest.raises(ValueError, match="Ссылки"):
        stage(tmp_path, archive)


def test_directory_symlink_rejected_even_if_excluded(tmp_path: Path) -> None:
    source = payload(tmp_path / "source")
    (source / ".env").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="Ссылки"):
        stage(tmp_path, source)


@pytest.mark.parametrize("names", [["a", "a"], ["a", "A"], ["A/x", "a/y"]])
def test_duplicate_and_case_collisions_rejected(tmp_path: Path, names: list[str]) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in names:
            output.writestr(name, "x")
    with pytest.raises(ValueError):
        stage(tmp_path, archive)


@pytest.mark.parametrize("limit,value", [("MAX_ENTRIES", 2), ("MAX_UNPACKED", 1), ("MAX_ARCHIVE", 1)])
def test_resource_limits_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str, value: int) -> None:
    archive = tmp_path / "large.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name in ("a", "b", "c"):
            output.writestr(name, "a" * 1000)
    monkeypatch.setattr(release, limit, value)
    with pytest.raises(ValueError):
        stage(tmp_path, archive)
    assert not list((tmp_path / ".alla-releases").iterdir())


@pytest.mark.parametrize("damage", ["file", "manifest", "complete", "dirlink"])
def test_existing_corrupt_release_is_preserved_and_rejected(tmp_path: Path, damage: str) -> None:
    source = payload(tmp_path / "source")
    staged, _ = stage(tmp_path, source)
    if damage == "file":
        (staged / "SKILL.md").write_text("corrupted")
    elif damage == "manifest":
        (staged.parent / "manifest.json").write_text(json.dumps({}))
    elif damage == "complete":
        (staged.parent / "complete").unlink()
    else:
        (staged / "outside").symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError):
        stage(tmp_path, source)
    assert staged.exists()


@pytest.mark.parametrize("args", [["--source", "https://host/release.zip"], ["--source", "git@host:repo"], ["--ref", "main"]])
def test_network_sources_and_ref_rejected(tmp_path: Path, args: list[str]) -> None:
    with pytest.raises(ValueError):
        release.source_argument(args, tmp_path)


def test_legacy_configuration_requires_migration(tmp_path: Path) -> None:
    (tmp_path / "alla-extensions").mkdir()
    config = tmp_path / "alla-extensions/config.toml"
    config.write_text('[update]\nrepo = "https://host/repo"\n')
    with pytest.raises(ValueError, match="Старая"):
        release.source_argument([], tmp_path)
    config.write_text('[update]\nsource = "release.zip"\n')
    assert release.source_argument([], tmp_path) == tmp_path / "release.zip"


def test_entrypoint_update_runs_without_environment_and_keeps_running_files(tmp_path: Path) -> None:
    active = payload(tmp_path / ".qwen/skills/alla-launch", "old")
    scripts = Path(release.__file__).parent
    for name in ("alla_skill.py", "alla_release.py"):
        shutil.copyfile(scripts / name, active / "scripts" / name)
    before = (active / "scripts/alla_skill.py").read_bytes()
    source = payload(tmp_path / "source")
    result = subprocess.run([sys.executable, str(active / "scripts/alla_skill.py"),
                             "update", "--source", str(source)], cwd=tmp_path,
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("STATUS: update_ready")
    assert (active / "scripts/alla_skill.py").read_bytes() == before
    assert not (tmp_path / ".venv").exists()
    assert not (tmp_path / ".qwen/commands").exists()
    assert len(list((tmp_path / ".alla-releases").glob("*/complete"))) == 1


@pytest.mark.parametrize("name", [".env", ".env-backup", ".env_old", ".venv/token", "__pycache__/code.pyc"])
def test_existing_release_rejects_added_excluded_payload(tmp_path: Path, name: str) -> None:
    source = payload(tmp_path / "source")
    staged, _ = stage(tmp_path, source)
    added = staged / name
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_text("unexpected")
    with pytest.raises(ValueError, match="исключённый"):
        stage(tmp_path, source)
    assert added.read_text() == "unexpected"


@pytest.mark.parametrize("name", [".env-backup", ".env_old"])
def test_source_env_backup_variants_are_excluded(tmp_path: Path, name: str) -> None:
    source = payload(tmp_path / "source")
    (source / name).write_text("sensitive-backup")
    staged, diff = stage(tmp_path, source)
    assert not (staged / name).exists()
    assert "sensitive-backup" not in diff.read_text()


@pytest.mark.parametrize("damage", ["crc", "encrypted", "truncated_tar"])
def test_corrupt_archive_members_are_reported_as_validation_errors(tmp_path: Path, damage: str) -> None:
    archive = tmp_path / "damaged.archive"
    if damage == "truncated_tar":
        entry = tarfile.TarInfo("payload.txt")
        entry.size = 10000
        archive.write_bytes(entry.tobuf() + b"partial")
    else:
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("payload.txt", b"unique-content")
        data = bytearray(archive.read_bytes())
        if damage == "crc":
            offset = data.index(b"unique-content")
            data[offset] ^= 1
        else:
            local = data.index(b"PK\x03\x04")
            central = data.index(b"PK\x01\x02")
            data[local + 6] |= 1
            data[central + 8] |= 1
        archive.write_bytes(data)
    with pytest.raises(ValueError, match="Повреждённый или зашифрованный"):
        stage(tmp_path, archive)
    assert not list((tmp_path / ".alla-releases").iterdir())


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipe")
def test_special_root_source_is_rejected_without_opening_or_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "release.fifo"
    os.mkfifo(fifo)
    entry = Path(release.__file__).parent / "alla_skill.py"
    result = subprocess.run([sys.executable, str(entry), "update", "--source", str(fifo)],
                            cwd=tmp_path, text=True, capture_output=True, check=False, timeout=3)
    assert result.returncode == 2
    assert result.stdout.startswith("STATUS: error")
    assert "обычным файлом" in result.stdout
    assert "Traceback" not in result.stderr
    assert not list((tmp_path / ".alla-releases").iterdir())


@pytest.mark.parametrize("boundary", ["file", "directory", "root", "ancestor"])
def test_source_swap_to_symlink_is_rejected_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    container = tmp_path / "input"
    source = payload(container / "source")
    outside = payload(tmp_path / "outside", "SYNTHETIC_SECRET_CANARY")
    (source / "00-file.txt").write_text("original")
    (source / "00-dir").mkdir()
    (source / "00-dir" / "file.txt").write_text("original")
    victims = {"file": source / "00-file.txt", "directory": source / "00-dir",
               "root": source, "ancestor": container}
    victim = victims[boundary]
    moved = victim.with_name(victim.name + "-old")
    swapped = False
    original_open = os.open
    original_path_open = Path.open
    original_scandir = os.scandir
    original_resolve = Path.resolve

    def swap() -> None:
        nonlocal swapped
        if not swapped:
            victim.rename(moved)
            victim.symlink_to(outside / "SKILL.md" if boundary == "file" else outside,
                              target_is_directory=boundary != "file")
            swapped = True

    def open_entry(name, flags, *args, **kwargs):
        if str(name) == victim.name and "dir_fd" in kwargs:
            swap()
        return original_open(name, flags, *args, **kwargs)

    def open_path(path, *args, **kwargs):
        if boundary == "file" and path == victim and args and args[0] == "rb":
            swap()
        return original_path_open(path, *args, **kwargs)

    def scandir(path):
        if boundary in {"directory", "root"} and not isinstance(path, int) and Path(path) == victim:
            swap()
        return original_scandir(path)

    def resolve(path, *args, **kwargs):
        if boundary == "ancestor" and path == source.parent:
            swap()
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_entry)
    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises((ValueError, OSError)):
        stage(tmp_path, source)
    assert swapped
    assert not list((tmp_path / ".alla-releases").iterdir())
    assert (tmp_path / "active/scripts/alla_skill.py").read_text() == "# old"


def test_untrusted_symlink_parent_is_not_resolved(tmp_path: Path) -> None:
    source = payload(tmp_path / "actual")
    alias = tmp_path / "user-alias"
    alias.symlink_to(source.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="Родитель"):
        stage(tmp_path, alias / source.name)
    assert not list((tmp_path / ".alla-releases").iterdir())


@pytest.mark.skipif(not Path("/tmp").is_symlink(), reason="System /tmp alias")
def test_trusted_system_tmp_alias_is_supported(tmp_path: Path) -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="alla-source-alias-", dir="/tmp") as directory:
        source = payload(Path(directory) / "source")
        # tempfile preserves the caller's /tmp spelling on macOS.
        assert source.parts[1] == "tmp"
        staged, _ = stage(tmp_path, source)
        assert (staged / "scripts/alla_skill.py").read_text() == "# new"


@pytest.mark.parametrize("kind", ["zip", "tar", "tgz"])
def test_archive_path_is_never_reopened_after_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    normal = payload(tmp_path / "normal", "normal")
    outside = payload(tmp_path / "outside", "SYNTHETIC_SECRET_CANARY")

    def pack(source: Path, destination: Path) -> None:
        if kind == "zip":
            with zipfile.ZipFile(destination, "w") as output:
                for path in source.rglob("*"):
                    if path.is_file():
                        output.write(path, path.relative_to(source).as_posix())
        else:
            with tarfile.open(destination, "w:gz" if kind == "tgz" else "w") as output:
                for path in source.rglob("*"):
                    output.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)

    archive = tmp_path / ("release." + kind)
    evil = tmp_path / ("outside." + kind)
    pack(normal, archive)
    pack(outside, evil)
    original = zipfile.is_zipfile
    swapped = False

    def probe(candidate):
        nonlocal swapped
        if not swapped:
            archive.unlink()
            archive.symlink_to(evil)
            swapped = True
        return original(candidate)

    monkeypatch.setattr(zipfile, "is_zipfile", probe)
    staged, diff = stage(tmp_path, archive)
    assert swapped
    assert (staged / "scripts/alla_skill.py").read_text() == "# normal"
    assert "SYNTHETIC_SECRET_CANARY" not in diff.read_text()


def test_archive_growing_while_read_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("payload", "normal")
    original_size = archive.stat().st_size
    inode = archive.stat().st_ino
    monkeypatch.setattr(release, "MAX_ARCHIVE", original_size + 1)
    original = os.fdopen
    grew = False

    class GrowingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size):
            nonlocal grew
            data = self.stream.read(size)
            if not grew:
                with archive.open("ab") as output:
                    output.write(b"growing")
                grew = True
            return data

    def fdopen(descriptor, *args, **kwargs):
        stream = original(descriptor, *args, **kwargs)
        return GrowingReader(stream) if os.fstat(descriptor).st_ino == inode else stream

    monkeypatch.setattr(os, "fdopen", fdopen)
    with pytest.raises(ValueError, match="Архив больше"):
        stage(tmp_path, archive)
    assert grew
    assert not list((tmp_path / ".alla-releases").iterdir())


def test_excluded_regular_files_are_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = payload(tmp_path / "source")
    (source / ".env").write_text("SYNTHETIC_SECRET_CANARY")
    original = os.open
    original_path_open = Path.open

    def no_secret_open(name, *args, **kwargs):
        assert str(name) != ".env"
        return original(name, *args, **kwargs)

    def no_secret_path_open(path, *args, **kwargs):
        if path.name == ".env" and args and args[0] == "rb":
            pytest.fail("excluded .env was opened")
        return original_path_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_secret_open)
    monkeypatch.setattr(Path, "open", no_secret_path_open)
    staged, diff = stage(tmp_path, source)
    assert not (staged / ".env").exists()
    assert "SYNTHETIC_SECRET_CANARY" not in diff.read_text()


def test_failed_collection_closes_every_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = payload(tmp_path / "source")
    (source / "00-folder").mkdir()
    (source / "00-folder" / "fail.txt").write_text("payload")
    opened = set()
    original_open = os.open
    original_add = release.Collector.add

    def track_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def fail_add(collector, name, *args, **kwargs):
        if name.endswith("fail.txt"):
            raise ValueError("injected collection failure")
        return original_add(collector, name, *args, **kwargs)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(release.Collector, "add", fail_add)
    with pytest.raises(ValueError, match="injected"):
        stage(tmp_path, source)
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert not list((tmp_path / ".alla-releases").iterdir())


@pytest.mark.parametrize("kind", ["payload", "archive"])
def test_hardlinked_source_files_are_rejected(tmp_path: Path, kind: str) -> None:
    source = payload(tmp_path / "source")
    if kind == "payload":
        outside = tmp_path / "outside"
        outside.write_text("SYNTHETIC_SECRET_CANARY")
        os.link(outside, source / "linked.txt")
        candidate = source
    else:
        outside = tmp_path / "outside.zip"
        with zipfile.ZipFile(outside, "w") as output:
            output.writestr("payload", "normal")
        candidate = tmp_path / "hardlink.zip"
        os.link(outside, candidate)
    with pytest.raises(ValueError, match="Ссылки"):
        stage(tmp_path, candidate)
    assert not list((tmp_path / ".alla-releases").iterdir())

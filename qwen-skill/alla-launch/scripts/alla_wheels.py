"""Expose only local wheel files to pip; never expose find-links HTML pages."""
from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextlib.contextmanager
def local_wheels(wheelhouse: Path) -> Iterator[Path]:
    """Snapshot regular .whl files into an isolated find-links directory.

    pip parses HTML files even when find-links points at a local directory and
    no-index is set. Copying only wheels also prevents newly added source HTML
    from becoming a pip candidate between validation and installation.
    """
    with tempfile.TemporaryDirectory(prefix="alla-local-wheels-") as temporary:
        destination = Path(temporary)
        count = 0
        for source in sorted(wheelhouse.iterdir()):
            if source.suffix.lower() != ".whl":
                continue
            if source.is_symlink():
                raise ValueError("Wheelhouse: ссылки на wheels запрещены: " + source.name)
            if not stat.S_ISREG(source.stat().st_mode):
                raise ValueError("Wheelhouse: нужен обычный .whl файл: " + source.name)
            descriptor = os.open(str(source), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as incoming:
                if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                    raise ValueError("Wheelhouse: нужен обычный .whl файл: " + source.name)
                with (destination / source.name).open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
            count += 1
        if not count:
            raise ValueError("Wheelhouse: в каталоге нет локальных .whl файлов")
        yield destination

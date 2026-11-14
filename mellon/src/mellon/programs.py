"""Find the external programs Mellon runs: the PDF program and draw.io desktop.

A program is looked up by name in the absolute ``PATH`` entries only. Windows
would otherwise look in the current folder first, and an MCP server usually
starts in the repository, so a program committed there would run on the
machine of whoever exports. So the current folder, relative ``PATH`` entries
and anything inside the repository are left out. A program the user names in a
setting (``MELLON_TECTONIC``, ``MELLON_DRAWIO``) is the caller's business and
is used wherever it is.

Standard library only: the frozen ``mellon-mcp.exe`` uses this too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# What Windows tries after a bare name when PATHEXT is unset.
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


def _full(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


# Requirements: MELLON-PDF-SRS-67, MELLON-EDIT-SRS-247
def allowed(path: Path | str, repo_root: Path | str | None = None) -> bool:
    """Whether Mellon may run the program file ``path``: not inside the
    repository at ``repo_root`` and not directly in the current folder.

    The current folder is the folder itself, not the folders under it: the
    installed build starts in its own folder and runs the PDF program from the
    ``tools`` folder under it.
    """
    full = _full(Path(path))
    if full is None:
        return False
    if repo_root is not None:
        root = _full(Path(repo_root))
        if root is not None and full.is_relative_to(root):
            return False
    try:
        cwd = _full(Path.cwd())
    except OSError:  # the current folder was deleted
        cwd = None
    return cwd is None or full.parent != cwd


def _file_names(name: str) -> list[str]:
    if sys.platform != "win32":
        return [name]
    exts = [e for e in (os.environ.get("PATHEXT") or _DEFAULT_PATHEXT).split(";") if e]
    if any(name.lower().endswith(e.lower()) for e in exts):
        return [name]
    return [name + e for e in exts]


def _runnable(path: Path) -> bool:
    if not path.is_file():
        return False
    return sys.platform == "win32" or os.access(path, os.X_OK)


# Requirements: MELLON-PDF-SRS-67, MELLON-EDIT-SRS-247
def which(name: str, repo_root: Path | str | None = None) -> str | None:
    """The first program called ``name`` in the absolute ``PATH`` entries
    that :func:`allowed` lets Mellon run, or None.

    Unlike ``shutil.which`` this never looks in the current folder, which
    Windows searches first for a bare name.
    """
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry:
            continue
        folder = Path(entry)
        if not folder.is_absolute():
            continue
        for file_name in _file_names(name):
            candidate = folder / file_name
            if _runnable(candidate) and allowed(candidate, repo_root):
                return str(candidate)
    return None

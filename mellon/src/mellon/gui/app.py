"""Entry point for `mellon-gui`.

Usage:
    mellon-gui [path]

If `path` is given, it's loaded as the initial repo. Otherwise the Project
Explorer opens empty and the user picks a repo via File → Open Repo.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QStandardPaths, Qt, QThread, QThreadPool
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QApplication

from .. import __version__
from . import branding
from .project_explorer_window import ProjectExplorerWindow
from .theme import ThemeManager
from . import plain


# Requirements: MELLON-GUI-SRS-24
_ERROR_DIALOG_INTERVAL_S = 10.0


# Requirements: MELLON-GUI-SRS-22
def _log_folder() -> Path:
    r"""The folder the error log goes in.

    On Windows (MELLON-GUI-SRS-21, -22) `%LOCALAPPDATA%\<org>\<app>`, which
    is `%LOCALAPPDATA%\mellon\mellon`, read from the variable rather than
    the known folder, so the place support asks for is the one the variable
    names; `%TEMP%` if the variable is unset or isn't a folder. Elsewhere,
    Qt's app-local data folder, or the temp folder if Qt can't determine it.
    Never a path relative to the working directory.
    """
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local and os.path.isabs(local) and os.path.isdir(local):
            return Path(local).joinpath(
                *(n for n in (QCoreApplication.organizationName(),
                              QCoreApplication.applicationName()) if n)
            )
        temp = os.environ.get("TEMP", "")
        if temp and os.path.isabs(temp) and os.path.isdir(temp):
            return Path(temp)
        return Path(tempfile.gettempdir())
    return Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        or tempfile.gettempdir()
    )


# Requirements: MELLON-GUI-SRS-20, MELLON-GUI-SRS-21, MELLON-GUI-SRS-24, MELLON-GUI-SRS-26
def _install_excepthook() -> Path:
    """Log uncaught exceptions to a file and show them in a dialog.

    PySide hands exceptions raised in slots to `sys.excepthook`; a windowed
    (frozen) build has no console (`sys.stderr` is None), so without this they
    vanish. Dialogs are rate-limited because a failing paint or `data()` call
    repeats on every repaint. Returns the log file path.
    """
    log_path = _log_folder() / "logs" / "mellon-gui.log"
    state = {"open": False, "last": float("-inf")}

    def hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        if sys.stderr is not None:
            sys.stderr.write(text)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"--- {datetime.now().isoformat(timespec='seconds')}\n{text}\n")
        except OSError:
            pass
        app = QApplication.instance()
        if app is None or QThread.currentThread() is not app.thread():
            return  # widgets only on the GUI thread; the log has it
        now = time.monotonic()
        if state["open"] or now - state["last"] < _ERROR_DIALOG_INTERVAL_S:
            return
        state["open"], state["last"] = True, now
        try:
            plain.critical(
                None,
                "Unexpected error",
                f"{exc_type.__name__}: {exc}\n\nDetails were written to:\n{log_path}",
            )
        finally:
            state["open"] = False

    sys.excepthook = hook
    return log_path


# Requirements: MELLON-GUI-SRS-2, MELLON-GUI-SRS-7, MELLON-GUI-SRS-8, MELLON-GUI-SRS-127
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mellon-gui")
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        help="Path to a mellon repository root (folder containing document folders).",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    args = parser.parse_args(argv)

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("mellon")
    app.setApplicationDisplayName("Mellon")
    app.setApplicationVersion(__version__)
    app.setOrganizationName(ThemeManager.SETTINGS_ORG)
    # After the names are set: they pick the AppLocalDataLocation folder.
    _install_excepthook()

    font = QFont()
    font.setFamilies(["Inter", "Segoe UI", "system-ui", "sans-serif"])
    font.setPointSize(10)
    app.setFont(font)

    theme = ThemeManager.instance()
    theme.apply(theme.load_choice(), persist=False)

    app.setWindowIcon(branding.app_icon())

    window = ProjectExplorerWindow()
    window.show()

    if args.path is not None:
        window.open_repo(args.path.resolve())

    rc = app.exec()
    # A PDF export still running (the app was quit mid-export) finishes now,
    # while the interpreter and its temp folder are intact; left to Python's
    # exit, the temp folder is deleted under tectonic and the PDF is lost.
    QThreadPool.globalInstance().waitForDone()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

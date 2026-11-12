"""Tests for the `mellon-gui` entry point (`gui/app.py`)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _raise_into(hook, message: str) -> None:
    try:
        raise ValueError(message)
    except ValueError as e:
        hook(type(e), e, e.__traceback__)


def test_excepthook_logs_to_file_and_rate_limits_dialog(qtbot, tmp_path: Path, monkeypatch):
    from mellon.gui import app as gui_app

    # Where the log goes is MELLON-GUI-SRS-21/-22's (test_gui_srs_21/22); here
    # only that the hook logs and rate-limits, so never into the real folder.
    monkeypatch.setattr(gui_app, "_log_folder", lambda: tmp_path)
    shown: list[tuple] = []
    monkeypatch.setattr(
        gui_app.QMessageBox, "critical", lambda *args, **_kwargs: shown.append(args)
    )
    previous = sys.excepthook
    try:
        log_path = gui_app._install_excepthook()
        hook = sys.excepthook
    finally:
        sys.excepthook = previous

    # A windowed (frozen) exe has no stderr; the hook must cope.
    with monkeypatch.context() as m:
        m.setattr(sys, "stderr", None)
        _raise_into(hook, "first failure")
        _raise_into(hook, "repeat failure")

    assert log_path == tmp_path / "logs" / "mellon-gui.log"
    log = log_path.read_text(encoding="utf-8")
    assert "ValueError: first failure" in log
    assert "ValueError: repeat failure" in log
    # One dialog for the burst; both tracebacks still logged.
    assert len(shown) == 1
    assert "first failure" in shown[0][2]
    assert str(log_path) in shown[0][2]


def test_excepthook_log_falls_back_to_temp_dir(tmp_path: Path, monkeypatch):
    """An undetermined app-data folder (Qt's is "" elsewhere, %LOCALAPPDATA%
    unset on Windows) must not put the log in the current working directory."""
    from mellon.gui import app as gui_app

    monkeypatch.setattr(gui_app.QStandardPaths, "writableLocation", lambda _location: "")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(gui_app.tempfile, "gettempdir", lambda: str(tmp_path))
    previous = sys.excepthook
    try:
        log_path = gui_app._install_excepthook()
    finally:
        sys.excepthook = previous

    assert log_path == tmp_path / "logs" / "mellon-gui.log"


def test_main_waits_for_a_running_export_before_returning(qtbot, monkeypatch):
    """Quitting mid-export: `main` must let the export job finish before
    returning. Left to Python's exit, the export's temp folder is deleted
    under tectonic and the PDF is lost."""
    import threading
    import time
    from types import SimpleNamespace

    from PySide6.QtCore import QRunnable, QThreadPool

    from mellon.gui import app as gui_app

    finished = threading.Event()

    class _SlowExport(QRunnable):
        def run(self) -> None:
            time.sleep(0.3)
            finished.set()

    class _App:
        """Stands in for the QApplication: `exec` returns at once, as when
        the user closes the last window while an export runs."""

        def __getattr__(self, _name):
            return lambda *_a, **_k: None

        def exec(self) -> int:
            QThreadPool.globalInstance().start(_SlowExport())
            return 0

    class _Window:
        def show(self) -> None:
            pass

    class _Theme:
        def load_choice(self):
            return None

        def apply(self, *_a, **_k) -> None:
            pass

    fake_app = _App()
    monkeypatch.setattr(gui_app.QApplication, "instance", staticmethod(lambda: fake_app))
    monkeypatch.setattr(
        gui_app.QGuiApplication,
        "setHighDpiScaleFactorRoundingPolicy",
        staticmethod(lambda *_a: None),
    )
    monkeypatch.setattr(gui_app, "ProjectExplorerWindow", _Window)
    monkeypatch.setattr(
        gui_app,
        "ThemeManager",
        SimpleNamespace(SETTINGS_ORG="mellon", instance=lambda: _Theme()),
    )
    monkeypatch.setattr(gui_app, "branding", SimpleNamespace(app_icon=lambda: None))
    monkeypatch.setattr(gui_app, "_install_excepthook", lambda: None)

    assert gui_app.main([]) == 0
    assert finished.is_set()


# MELLON-EDIT-SRS blind tests (1-175)
#
# Written blind from MELLON-EDIT-SRS and the public signatures (docs/specification.md,
# step 7). Helpers are in tests/edit_support.py, which builds on tests/store_support.py.

from PySide6.QtGui import QAction  # noqa: E402

from mellon.loader import load_document  # noqa: E402
from tests.edit_support import (  # noqa: E402
    basic_repo,
    commit_all,  # noqa: F401, F811 -- fixture
    explorer,
    explorer_doc_ids,
    manifest_root,
    modals,  # noqa: F401, F811 -- fixture
    new_document_handler,
)
from tests.store_support import git_env  # noqa: E402,F401,F811


def test_edit_srs_18_project_explorer_new_document_creates_it(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-18: the Project Explorer's New document action, given NEWDOC,
    creates the folder NEWDOC with a manifest whose document ID is NEWDOC and no objects,
    and the Project Explorer then lists NEWDOC."""
    from mellon.gui.dialogs.new_document import NewDocumentDialog
    root = basic_repo(tmp_path)
    commit_all(root)
    pe = explorer(qtbot, root)
    handler = modals.handlers[NewDocumentDialog] = new_document_handler("NEWDOC")
    action = [a for a in pe.findChildren(QAction) if a.shortcut().toString() == "Ctrl+N"]
    assert action, "the Project Explorer has no New document action (Ctrl+N)"
    action[0].trigger()
    qtbot.wait(100)
    assert handler.ran, "the New document dialog wasn't filled in"
    manifest = root / "NEWDOC" / "NEWDOC.xml"
    assert manifest.is_file()
    assert manifest_root(root, "NEWDOC").get("id") == "NEWDOC"
    assert load_document(root / "NEWDOC").objects == {}
    assert "NEWDOC" in explorer_doc_ids(pe)


def test_edit_srs_18_project_explorer_refuses_document_id_with_space(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-18 with MELLON-EDIT-SRS-21: entering `bad id` creates no document
    and no folder."""
    from mellon.gui.dialogs.new_document import NewDocumentDialog
    root = basic_repo(tmp_path)
    commit_all(root)
    pe = explorer(qtbot, root)
    entries = sorted(p.name for p in root.iterdir())
    handler = modals.handlers[NewDocumentDialog] = new_document_handler("bad id")
    [a for a in pe.findChildren(QAction) if a.shortcut().toString() == "Ctrl+N"][0].trigger()
    qtbot.wait(100)
    assert handler.ran, "the New document dialog wasn't filled in"
    assert sorted(p.name for p in root.iterdir()) == entries
    assert "bad" not in explorer_doc_ids(pe)

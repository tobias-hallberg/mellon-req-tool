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

    monkeypatch.setattr(
        gui_app.QStandardPaths, "writableLocation", lambda _location: str(tmp_path)
    )
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
    """An undetermined app-data folder ("") must not put the log in the
    current working directory."""
    from mellon.gui import app as gui_app

    monkeypatch.setattr(gui_app.QStandardPaths, "writableLocation", lambda _location: "")
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

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
        gui_app.plain.QMessageBox, "critical", lambda *args, **_kwargs: shown.append(args)
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
    explorer,
    explorer_doc_ids,
    manifest_root,
    new_document_handler,
)


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


# MELLON-GUI-SRS blind tests
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from importlib.metadata import version as dist_version  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QStandardPaths, QTimer, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit, QTabWidget  # noqa: E402

from tests.gui_support import (  # noqa: E402
    DOC_IDS,
    SRS,
    STS,
    _RELEASE_NUMBER,
    _commit_accepting,
    _doc_dirty,
    _explorer_of_main,
    _fake_tectonic,
    _head,
    _mcp_script,
    _obj_file,
    _offscreen_env,
    _start_export,
    _visible_explorers,
    _write,
)


def test_gui_srs_2_folder_argument_opens_repository(gui_gui, repo, run_main):
    """MELLON-GUI-SRS-2: started with a folder path as the argument, the GUI opens the folder as the repository."""
    run_main([str(repo)])
    pes = _visible_explorers()
    assert len(pes) == 1, f"expected one Project Explorer, got {len(pes)}"
    assert sorted(gui_gui.listed_docs(pes[0])) == sorted(DOC_IDS)


def test_gui_srs_2_no_argument_opens_no_repository(gui_gui, run_main):
    """MELLON-GUI-SRS-2 (control): started without a folder argument, the Project Explorer lists no document."""
    run_main([])
    pes = _visible_explorers()
    assert len(pes) == 1
    assert gui_gui.listed_docs(pes[0]) == []


def test_gui_srs_7_version_exits_with_status_0(gui_version_run):
    """MELLON-GUI-SRS-7: mellon-gui --version exits with status 0 (within 30 s)."""
    p, elapsed = gui_version_run
    assert p.returncode == 0, p.stderr
    assert elapsed < 30


def test_gui_srs_7_version_opens_no_window(gui_gui, run_main, monkeypatch):
    """MELLON-GUI-SRS-7: with --version the entry point exits with status 0 without opening a window."""
    from mellon.gui import project_explorer_window

    built = []
    real_init = project_explorer_window.ProjectExplorerWindow.__init__

    def counting_init(self, *a, **k):
        built.append(1)
        real_init(self, *a, **k)

    monkeypatch.setattr(project_explorer_window.ProjectExplorerWindow, "__init__", counting_init)
    shown_before = {id(w) for w in QApplication.topLevelWidgets() if w.isVisible()}
    rc, _ = run_main(["--version"])
    assert rc == 0
    assert built == [], "a Project Explorer was built"
    assert run_main.state["exec_calls"] == 0, "the GUI's event loop ran"
    assert [w for w in QApplication.topLevelWidgets() if w.isVisible() and id(w) not in shown_before] == []


def test_gui_srs_8_version_writes_release_number(gui_version_run):
    """MELLON-GUI-SRS-8: --version writes the release number (form 0.3.0) to standard output,
    the same number the MCP server reports and this build carries."""
    p, _ = gui_version_run
    out = p.stdout.decode("utf-8")
    numbers = _RELEASE_NUMBER.findall(out)
    assert numbers == [dist_version("mellon")], f"stdout {out!r}"
    assert out.endswith("\n")
    mcp = subprocess.run(_mcp_script() + ["--version"], capture_output=True, timeout=60)
    assert _RELEASE_NUMBER.findall(mcp.stdout.decode("utf-8")) == numbers


def test_gui_srs_13_quit_closes_every_window_and_ends(gui_gui, repo, run_main):
    """MELLON-GUI-SRS-13: Quit in the Project Explorer closes each GUI window and ends the GUI."""
    seen = {}

    def scenario(result):
        pe = _explorer_of_main()
        gui_gui.open_doc(pe, SRS)
        gui_gui.open_doc(pe, STS)
        seen["windows"] = len(gui_gui.editors()) + 1
        gui_gui.action(pe, "Quit").trigger()

    rc, result = run_main([str(repo)], scenario)
    assert seen.get("windows") == 3
    assert not result.get("timed_out"), "the GUI's event loop did not end after Quit"
    assert [w for w in QApplication.topLevelWidgets() if w.isVisible()] == []


def test_gui_srs_14_closing_explorer_closes_editors_and_ends(gui_gui, repo, run_main):
    """MELLON-GUI-SRS-14: closing the Project Explorer closes each Document Editor and ends the GUI."""
    seen = {}

    def scenario(result):
        pe = _explorer_of_main()
        gui_gui.open_doc(pe, SRS)
        gui_gui.open_doc(pe, STS)
        seen["editors"] = len(gui_gui.editors())
        pe.close()

    rc, result = run_main([str(repo)], scenario)
    assert seen.get("editors") == 2
    assert not result.get("timed_out"), "the GUI's event loop did not end after the Project Explorer closed"
    assert gui_gui.editors() == []


# --------------------------------------------------------------------------- unexpected errors


def _logs_with(marker: str, org: str) -> list[Path]:
    """Every mellon-gui.log, under the test's own app-data folder or the temp folder, that holds `marker`."""
    base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation))
    candidates = list((base / org).rglob("mellon-gui.log"))
    candidates += list((Path(os.environ.get("TEMP", "")) / "logs").glob("mellon-gui.log"))
    return [p for p in candidates if p.is_file() and marker in p.read_text(encoding="utf-8", errors="replace")]


def _raise_in_loop(qtbot, exc: BaseException):
    def boom():
        raise exc

    QTimer.singleShot(0, boom)
    qtbot.wait(60)


def _error_dialogs(gui_gui) -> list[tuple[str, str, str, str]]:
    return [e for e in gui_gui.log if "error" in (e[2] + e[3]).lower()]


@pytest.mark.qt_no_exception_capture
def test_gui_srs_20_error_in_event_loop_is_logged(gui_gui, repo, run_main, qtbot):
    """MELLON-GUI-SRS-20: an unexpected error in the GUI is appended to the error log, with its type,
    message and stack trace."""
    run_main([str(repo)])
    marker = f"blind-20-{uuid.uuid4().hex[:8]}"
    _raise_in_loop(qtbot, LookupError(marker))
    logs = _logs_with(marker, run_main.org)
    assert logs, "the error is in no error log"
    text = logs[0].read_text(encoding="utf-8")
    assert "LookupError" in text and "Traceback" in text


@pytest.mark.qt_no_exception_capture
def test_gui_srs_20_error_before_first_window_is_logged(gui_gui, run_main, monkeypatch):
    """MELLON-GUI-SRS-20: an unexpected error raised before the first window shows is appended to the error log."""
    from mellon.gui import project_explorer_window

    marker = f"blind-20-start-{uuid.uuid4().hex[:8]}"

    def failing_init(self, *a, **k):
        raise LookupError(marker)

    monkeypatch.setattr(project_explorer_window.ProjectExplorerWindow, "__init__", failing_init)
    try:
        run_main([])
    except LookupError:
        # The interpreter hands an uncaught error to sys.excepthook; do what it does.
        sys.excepthook(*sys.exc_info())
    assert _logs_with(marker, run_main.org), "the start-up error is in no error log"


@pytest.mark.qt_no_exception_capture
def test_gui_srs_20_error_in_compare_job_is_logged(gui_gui, repo, run_main, qtbot, monkeypatch):
    """MELLON-GUI-SRS-20: an unexpected error inside the Compare background job is appended to the error log."""
    from mellon.service.document import DocumentService

    marker = f"blind-20-compare-{uuid.uuid4().hex[:8]}"

    def failing(self, *a, **k):
        raise LookupError(marker)

    monkeypatch.setattr(DocumentService, "compare_document", failing)
    run_main([str(repo)])
    ed = gui_gui.open_doc(_explorer_of_main(), SRS)
    gui_gui.action(ed, "Compare").trigger()
    qtbot.waitUntil(lambda: bool(_logs_with(marker, run_main.org)), timeout=10000)


@pytest.mark.qt_no_exception_capture
def test_gui_srs_20_error_in_history_job_is_logged(gui_gui, repo, run_main, qtbot, monkeypatch):
    """MELLON-GUI-SRS-20: an unexpected error while the History tab loads is appended to the error log."""
    from mellon.gui.controller import RepoController
    from mellon.service.document import DocumentService

    marker = f"blind-20-history-{uuid.uuid4().hex[:8]}"

    def failing(self, *a, **k):
        raise LookupError(marker)

    monkeypatch.setattr(RepoController, "object_history", failing)
    monkeypatch.setattr(DocumentService, "git_log", failing)
    run_main([str(repo)])
    ed = gui_gui.open_doc(_explorer_of_main(), SRS)
    gui_gui.select(ed, "VCU3-SRS-2")
    tabs = [t for t in ed.right_pane.findChildren(QTabWidget)][0]
    bar = tabs.tabBar()
    at = bar.tabRect([tabs.tabText(i) for i in range(tabs.count())].index("History")).center()
    # The user's click arrives from the event loop, as a real one does.
    QTimer.singleShot(0, lambda: QTest.mouseClick(bar, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at))
    qtbot.waitUntil(lambda: bool(_logs_with(marker, run_main.org)), timeout=10000)


_LOG_DRIVER = r'''
import json, sys
from pathlib import Path
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
ini = Path(sys.argv[1])
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(ini))
import mellon.gui.settings as settings
settings.make_settings = lambda: QSettings(str(ini / "settings.ini"), QSettings.Format.IniFormat)
shown = []
def _exec(box):
    shown.append(box.text() + "\n" + box.informativeText())
    return 0
QMessageBox.exec = _exec
def _static(*a, **k):
    shown.append(" ".join(str(x) for x in a[1:3]))
    return QMessageBox.StandardButton.Ok
for n in ("critical", "warning", "information", "question"):
    setattr(QMessageBox, n, staticmethod(_static))
real_exec = QApplication.exec
def fake_exec(*_):
    def boom():
        raise RuntimeError(sys.argv[2])
    QTimer.singleShot(0, boom)
    QTimer.singleShot(800, QApplication.quit)
    return real_exec()
QApplication.exec = fake_exec
from mellon.gui.app import main
rc = main([])
print(json.dumps({"rc": rc, "shown": shown}))
'''


def _real_log() -> Path:
    base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation))
    return base / "mellon" / "mellon" / "logs" / "mellon-gui.log"


@pytest.fixture
def log_driver(tmp_path):
    """Run the GUI in a subprocess that raises one unexpected error, keeping its settings in tmp_path.

    The user's own error log is put back as it was afterwards, in case the GUI writes there.
    """
    real = _real_log()
    saved = real.read_bytes() if real.exists() else None
    missing = [d for d in (real.parent, real.parent.parent, real.parent.parent.parent) if not d.exists()]
    script = tmp_path / "driver.py"
    _write(script, _LOG_DRIVER)
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    def run(env_changes: dict, drop: tuple[str, ...] = ()):
        marker = f"blind-log-{uuid.uuid4().hex[:8]}"
        env = _offscreen_env(**env_changes)
        for k in drop:
            env.pop(k, None)
        p = subprocess.run([sys.executable, str(script), str(tmp_path / "ini"), marker],
                           cwd=cwd, env=env, capture_output=True, timeout=120)
        return marker, p

    run.cwd = cwd
    yield run
    if saved is None:
        if real.exists():
            real.unlink()
    else:
        real.write_bytes(saved)
    for d in missing:  # deepest first: remove only the folders the GUI made, if empty
        try:
            d.rmdir()
        except OSError:
            pass


@pytest.mark.skipif(sys.platform != "win32", reason="MELLON-GUI-SRS-21 is the Windows location")
def test_gui_srs_21_error_log_in_localappdata(tmp_path, log_driver):
    """MELLON-GUI-SRS-21: on Windows the error log is %LOCALAPPDATA%\\mellon\\mellon\\logs\\mellon-gui.log."""
    lad = tmp_path / "lad"
    lad.mkdir()
    marker, p = log_driver({"LOCALAPPDATA": str(lad)})
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    log = lad / "mellon" / "mellon" / "logs" / "mellon-gui.log"
    assert log.is_file(), f"no error log at {log}; the GUI said: {p.stdout.decode(errors='replace')}"
    assert marker in log.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="%LOCALAPPDATA% is a Windows variable")
@pytest.mark.parametrize("case", ["unset", "names_a_file"])
def test_gui_srs_22_error_log_falls_back_to_temp(tmp_path, log_driver, case):
    """MELLON-GUI-SRS-22: if %LOCALAPPDATA% is unset or isn't a folder, the error log is %TEMP%\\logs\\mellon-gui.log,
    and nothing is written in the current folder."""
    temp = tmp_path / "temp"
    temp.mkdir()
    if case == "unset":
        marker, p = log_driver({"TEMP": str(temp), "TMP": str(temp)}, drop=("LOCALAPPDATA",))
    else:
        afile = tmp_path / "not-a-folder"
        _write(afile, "x")
        marker, p = log_driver({"TEMP": str(temp), "TMP": str(temp), "LOCALAPPDATA": str(afile)})
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    log = temp / "logs" / "mellon-gui.log"
    assert log.is_file() and marker in log.read_text(encoding="utf-8"), (
        f"the error is not in {log}; the GUI said: {p.stdout.decode(errors='replace')}")
    assert list(log_driver.cwd.iterdir()) == []


class _Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.mark.qt_no_exception_capture
def test_gui_srs_24_error_dialog_at_most_once_per_10_s(gui_gui, repo, run_main, qtbot, monkeypatch):
    """MELLON-GUI-SRS-24: a dialog naming the error log opens for an unexpected error only when no such dialog
    opened less than 10 s before: errors at 0, 9.9, 10.0, 10.1 and 20.2 s give dialogs at 0, 10.0 and 20.2 s."""
    run_main([str(repo)])
    clock = _Clock()
    for name in ("monotonic", "time", "perf_counter"):
        monkeypatch.setattr(time, name, clock)
    shown_at = []
    start = clock.now
    for offset in (0.0, 9.9, 10.0, 10.1, 20.2):
        clock.now = start + offset
        before = len(_error_dialogs(gui_gui))
        _raise_in_loop(qtbot, LookupError(f"blind-24-{offset}"))
        if len(_error_dialogs(gui_gui)) > before:
            shown_at.append(offset)
    for name in ("monotonic", "time", "perf_counter"):
        monkeypatch.setattr(time, name, getattr(time, name))
    assert shown_at == [0.0, 10.0, 20.2]


@pytest.mark.qt_no_exception_capture
def test_gui_srs_24_error_dialog_names_the_log_file(gui_gui, repo, run_main, qtbot):
    """MELLON-GUI-SRS-24: the unexpected-error dialog names the error log file by its full path."""
    run_main([str(repo)])
    marker = f"blind-24-path-{uuid.uuid4().hex[:8]}"
    _raise_in_loop(qtbot, LookupError(marker))
    dialogs = _error_dialogs(gui_gui)
    assert dialogs, "no dialog opened for the error"
    logs = _logs_with(marker, run_main.org)
    assert logs, "the error is in no error log"
    shown = dialogs[-1][3]
    assert str(logs[0]) in shown or str(logs[0]).replace("\\", "/") in shown, shown


@pytest.mark.qt_no_exception_capture
def test_gui_srs_26_gui_keeps_running_after_unexpected_error(gui_gui, repo, run_main, qtbot):
    """MELLON-GUI-SRS-26: after an unexpected error the GUI keeps running with its windows open:
    both windows stay shown, events are handled and a following grid edit is written."""
    run_main([str(repo)])
    pe = _explorer_of_main()
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    _raise_in_loop(qtbot, LookupError("blind-26"))
    assert _error_dialogs(gui_gui), "no dialog for the error"
    assert pe.isVisible() and ed.isVisible()
    fired = []
    QTimer.singleShot(0, lambda: fired.append(1))
    qtbot.waitUntil(lambda: fired == [1], timeout=2000)
    editor = gui_gui.open_cell_editor(ed, "VCU3-SRS-2", "Rationale")
    assert isinstance(editor, QLineEdit), f"no text editor opened on the cell, focus is {type(editor).__name__}"
    QTest.keyClicks(editor, "after-error")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: "after-error" in _obj_file(repo, SRS, "VCU3-SRS-2").read_text(encoding="utf-8"),
                    timeout=3000)


def test_gui_srs_217_edit_and_commit_without_drawio(gui_gui, repo, run_main, monkeypatch, tmp_path, qtbot):
    """MELLON-GUI-SRS-217: without draw.io desktop, the GUI starts, edits an attribute value and a
    description, and commits."""
    empty = tmp_path / "no-drawio"
    empty.mkdir()
    monkeypatch.delenv("MELLON_DRAWIO", raising=False)
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(empty))
    path = [p for p in os.environ.get("PATH", "").split(os.pathsep)
            if p and not any((Path(p) / n).exists() for n in ("drawio", "draw.io", "drawio.exe", "draw.io.exe"))]
    monkeypatch.setenv("PATH", os.pathsep.join(path))
    run_main([str(repo)])
    ed = gui_gui.open_doc(_explorer_of_main(), SRS, editing=True)
    gui_gui.select(ed, "VCU3-SRS-2")
    editor = gui_gui.open_cell_editor(ed, "VCU3-SRS-2", "Rationale")
    QTest.keyClicks(editor, "no-drawio-rationale")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    desc = gui_gui.insp_description(ed)
    desc.setFocus()
    QTest.keyClick(desc, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(desc, " No draw.io.")
    gui_gui.click_cell(ed, "VCU3-SRS-11")
    text = _obj_file(repo, SRS, "VCU3-SRS-2").read_text(encoding="utf-8")
    assert "no-drawio-rationale" in text and "No draw.io." in text
    head = _head(repo)
    _commit_accepting(gui_gui)
    gui_gui.action(ed, "Commit").trigger()
    qtbot.waitUntil(lambda: _head(repo) != head, timeout=5000)
    assert _doc_dirty(repo, SRS) == []


@pytest.mark.qt_no_exception_capture
def test_gui_srs_20_error_in_export_job_is_logged(gui_gui, repo, run_main, qtbot, monkeypatch, tmp_path):
    """MELLON-GUI-SRS-20: an unexpected error inside the export background job is appended to the error log.
    The error is raised where the job writes the chosen PDF file (any of the usual ways of writing a file, in a
    thread other than the GUI's)."""
    import threading

    marker = f"blind-20-export-{uuid.uuid4().hex[:8]}"
    out = tmp_path / "exported.pdf"

    def failing_in_job(owner, name):
        real = getattr(owner, name)

        def f(*a, **k):
            if threading.current_thread() is not threading.main_thread() and out.name in " ".join(map(str, a)):
                raise LookupError(marker)
            return real(*a, **k)

        monkeypatch.setattr(owner, name, f)

    for owner, name in ((shutil, "copyfile"), (shutil, "copy"), (shutil, "copy2"), (shutil, "move"),
                        (os, "replace"), (os, "rename"), (Path, "replace"), (Path, "rename"), (Path, "write_bytes")):
        failing_in_job(owner, name)
    monkeypatch.setenv("MELLON_TECTONIC", str(_fake_tectonic(tmp_path / "tools", 0)))
    run_main([str(repo)])
    ed = gui_gui.open_doc(_explorer_of_main(), SRS)
    _start_export(gui_gui, ed, out)
    qtbot.waitUntil(lambda: bool(_logs_with(marker, run_main.org)), timeout=15000)

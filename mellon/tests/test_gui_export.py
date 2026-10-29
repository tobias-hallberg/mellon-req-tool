"""Tests for the GUI's PDF export action.

The actual `tectonic` shell-out is monkeypatched away — we exercise the
plumbing (file dialog wiring, status messages, settings persistence,
TectonicError handling) and trust `test_export.py` for the rendering
pipeline.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


REQ_TEST = (Path(__file__).resolve().parents[2] / "req-test").resolve()


pytestmark = pytest.mark.skipif(
    not (REQ_TEST / ".git").exists(),
    reason="req-test fixture not available",
)


@pytest.fixture
def window(qtbot, tmp_path: Path, clean_worktree):
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    dst = tmp_path / "req-test"
    shutil.copytree(REQ_TEST, dst)
    clean_worktree(dst)
    svc = DocumentService(dst, cache=True)
    docs = sorted(svc.get_all_documents())
    w = DocumentEditorWindow(docs[0], svc)
    qtbot.addWidget(w)
    return w, dst


@pytest.fixture(autouse=True)
def link_warning(monkeypatch):
    """Answer the pre-export suspect-link warning with "Export anyway": the
    fixture's VCU3-SRS has suspect links, and the real box is modal. Tests
    read ``.calls`` or set ``.answer``; ``.original`` is the real method."""
    from types import SimpleNamespace

    from mellon.gui.main_window import DocumentEditorWindow

    state = SimpleNamespace(
        calls=[],
        answer=True,
        original=DocumentEditorWindow._ask_export_despite_links,
    )

    def fake(self, doc_id, problems):
        state.calls.append((doc_id, problems))
        return state.answer

    monkeypatch.setattr(DocumentEditorWindow, "_ask_export_despite_links", fake)
    return state


def _export_and_wait(qtbot, w) -> None:
    """Trigger Export and wait for its background worker to report back."""
    w._export_act.trigger()
    qtbot.waitUntil(lambda: not w.is_exporting(), timeout=10_000)


def _save_to(monkeypatch, target: Path) -> None:
    from mellon.gui import main_window as mw

    monkeypatch.setattr(
        mw.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: (str(target), "PDF (*.pdf)")),
    )


def test_export_action_visible_and_enabled(window):
    w, _ = window
    assert w._export_act.isEnabled()
    # Tooltip mentions PDF and the shortcut.
    assert "PDF" in w._export_act.toolTip()
    assert "Ctrl+E" in w._export_act.toolTip()


def test_export_writes_pdf_to_selected_path(window, tmp_path, monkeypatch, qtbot):
    from mellon.gui import main_window as mw

    w, _ = window
    target = tmp_path / "exported.pdf"
    fake_pdf = b"%PDF-1.7\n%fake\n%%EOF\n"

    monkeypatch.setattr(
        mw.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: (str(target), "PDF (*.pdf)")),
    )
    seen: dict[str, object] = {}

    def fake_pdf_render(doc, *, rev=None, **kwargs):
        seen["doc_id"] = doc.id
        seen["rev"] = rev
        return fake_pdf

    monkeypatch.setattr(mw, "export_pdf", fake_pdf_render)

    _export_and_wait(qtbot, w)

    assert target.exists()
    assert target.read_bytes() == fake_pdf
    # The doc passed to export was the one open in the window.
    assert seen["doc_id"] == w._doc_id


def test_export_passes_parents(window, tmp_path, monkeypatch, qtbot):
    """VCU3-SRS declares VCU3-SyRS: the export gets it for the PDF's
    Traceability section."""
    from mellon.gui import main_window as mw

    w, _ = window
    assert w._doc_id == "VCU3-SRS"
    _save_to(monkeypatch, tmp_path / "traced.pdf")
    seen: dict = {}

    def fake_pdf_render(doc, **kwargs):
        seen.update(kwargs)
        return b"%PDF-stub"

    monkeypatch.setattr(mw, "export_pdf", fake_pdf_render)

    _export_and_wait(qtbot, w)

    assert list(seen["parents"]) == ["VCU3-SyRS"]
    assert "statuses" not in seen


def test_export_warns_about_suspect_links_first(
    window, tmp_path, monkeypatch, qtbot, link_warning
):
    """The PDF shows no link states, so suspect links are checked before
    the save dialog; answering Cancel stops the export."""
    from mellon.gui import main_window as mw
    from mellon.suspects import LinkState

    w, _ = window
    link_warning.answer = False
    monkeypatch.setattr(
        mw.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: pytest.fail("no save dialog after Cancel")),
    )
    monkeypatch.setattr(
        mw, "export_pdf", lambda *a, **k: pytest.fail("no export after Cancel")
    )

    w._export_act.trigger()

    assert not w.is_exporting()
    assert "cancel" in w._status.currentMessage().lower()
    [(doc_id, problems)] = link_warning.calls
    assert doc_id == "VCU3-SRS"
    assert problems and all(s.state is LinkState.SUSPECT for s in problems)


def test_export_skips_warning_without_link_problems(
    window, tmp_path, monkeypatch, qtbot, link_warning
):
    from mellon.gui import main_window as mw

    w, _ = window
    monkeypatch.setattr(w, "_link_problems", lambda doc_id: [])
    _save_to(monkeypatch, tmp_path / "clean.pdf")
    monkeypatch.setattr(mw, "export_pdf", lambda doc, **k: b"%PDF-stub")

    _export_and_wait(qtbot, w)

    assert link_warning.calls == []
    assert (tmp_path / "clean.pdf").exists()


def test_link_warning_lists_links_and_defaults_to_cancel(
    window, monkeypatch, link_warning
):
    from mellon.gui import main_window as mw

    w, _ = window
    problems = w._link_problems("VCU3-SRS")
    many = problems * 3  # more than the box lists
    assert len(many) > mw._LINK_WARNING_ROWS
    seen: dict = {}

    def fake_exec(box):
        seen["text"] = box.text()
        seen["details"] = box.informativeText()
        seen["default"] = box.defaultButton()
        seen["cancel"] = box.button(mw.QMessageBox.StandardButton.Cancel)
        return 0  # closed without a click: clickedButton() is None

    monkeypatch.setattr(mw.QMessageBox, "exec", fake_exec)

    assert link_warning.original(w, "VCU3-SRS", many) is False
    assert f"{len(many)} suspect and 0 broken" in seen["text"]
    first = problems[0]
    assert (
        f"{first.source_id} → {first.link.target_doc}/{first.link.target_id}: suspect"
    ) in seen["details"]
    assert f"… and {len(many) - mw._LINK_WARNING_ROWS} more" in seen["details"]
    assert seen["default"] is seen["cancel"]


def test_export_includes_text_typed_just_before(window, tmp_path, monkeypatch, qtbot):
    """Ctrl+E and the toolbar button leave focus in the Inspector, so its
    focus-out commit comes too late: the export writes pending text first,
    so the PDF matches the screen."""
    from mellon.gui import main_window as mw

    w, root = window
    assert w._doc_id == "VCU3-SRS"
    w._set_read_only(False)
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")
    w.inspector._description.setPlainText("TYPED JUST BEFORE CTRL+E")
    _save_to(monkeypatch, tmp_path / "typed.pdf")
    seen: dict[str, str] = {}

    def fake_pdf_render(doc, **_kwargs):
        seen["text"] = doc.objects["VCU3-SRS-10"].description
        return b"%PDF-stub"

    monkeypatch.setattr(mw, "export_pdf", fake_pdf_render)

    _export_and_wait(qtbot, w)

    assert seen["text"] == "TYPED JUST BEFORE CTRL+E"
    obj = root / "VCU3-SRS" / "objects" / "VCU3-SRS-10.xml"
    assert "TYPED JUST BEFORE CTRL+E" in obj.read_text(encoding="utf-8")


def test_export_appends_pdf_extension_when_missing(window, tmp_path, monkeypatch, qtbot):
    from mellon.gui import main_window as mw

    w, _ = window
    chosen = tmp_path / "no-extension"  # user typed no extension
    expected = tmp_path / "no-extension.pdf"

    monkeypatch.setattr(
        mw.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: (str(chosen), "PDF (*.pdf)")),
    )
    monkeypatch.setattr(mw, "export_pdf", lambda doc, **k: b"%PDF-stub")

    _export_and_wait(qtbot, w)

    assert expected.exists()
    assert not chosen.exists()


def test_export_cancelled_when_dialog_returns_empty(window, monkeypatch):
    from mellon.gui import main_window as mw

    w, _ = window
    monkeypatch.setattr(
        mw.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: ("", "")),
    )
    called = {"n": 0}

    def fake_pdf_render(doc, **k):
        called["n"] += 1
        return b""

    monkeypatch.setattr(mw, "export_pdf", fake_pdf_render)

    w._export_act.trigger()

    assert not w.is_exporting()
    assert called["n"] == 0
    assert "cancel" in w._status.currentMessage().lower()


def test_export_surfaces_tectonic_error(window, tmp_path, monkeypatch, qtbot):
    from mellon.gui import main_window as mw

    w, _ = window
    target = tmp_path / "out.pdf"

    monkeypatch.setattr(
        mw.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: (str(target), "PDF (*.pdf)")),
    )

    def boom(doc, **k):
        raise mw.TectonicError("tectonic exploded")

    monkeypatch.setattr(mw, "export_pdf", boom)

    box_messages: list[str] = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "critical",
        staticmethod(
            lambda parent, title, text, *a, **k: box_messages.append(text) or 0
        ),
    )

    _export_and_wait(qtbot, w)

    assert any("tectonic exploded" in m for m in box_messages)
    assert not target.exists()


def test_export_remembers_last_directory(window, tmp_path, monkeypatch, qtbot):
    from mellon.gui import main_window as mw
    from mellon.gui.settings import make_settings

    w, _ = window
    target = tmp_path / "subdir" / "saved.pdf"
    target.parent.mkdir()

    monkeypatch.setattr(
        mw.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: (str(target), "PDF (*.pdf)")),
    )
    monkeypatch.setattr(mw, "export_pdf", lambda doc, **k: b"%PDF-stub")

    _export_and_wait(qtbot, w)

    saved_dir = make_settings().value(f"{mw._EXPORT_DIR_KEY}/{w._doc_id}")
    assert saved_dir == str(target.parent)


def test_export_runs_off_the_gui_thread_one_at_a_time(
    window, tmp_path, monkeypatch, qtbot
):
    """A slow tectonic run (first-time bundle download) must not freeze the
    window, and a second Export while one runs must not start another."""
    import threading

    from mellon.gui import main_window as mw

    w, _ = window
    target = tmp_path / "slow.pdf"
    _save_to(monkeypatch, target)
    release = threading.Event()
    calls: list[bool] = []

    def slow_pdf(doc, **k):
        calls.append(threading.current_thread() is threading.main_thread())
        release.wait(timeout=10)
        return b"%PDF-slow"

    monkeypatch.setattr(mw, "export_pdf", slow_pdf)

    w._export_act.trigger()
    qtbot.waitUntil(lambda: bool(calls), timeout=10_000)
    assert calls == [False]  # rendered on a worker thread
    assert w.is_exporting()
    assert not w._export_act.isEnabled()
    w._export_current_doc()  # e.g. a queued Ctrl+E
    assert "already running" in w._status.currentMessage()

    release.set()
    qtbot.waitUntil(lambda: not w.is_exporting(), timeout=10_000)
    assert calls == [False]
    assert target.read_bytes() == b"%PDF-slow"
    assert w._export_act.isEnabled()


def test_export_names_objects_whose_figures_are_not_rendered(
    window, tmp_path, monkeypatch, qtbot
):
    from mellon.gui import main_window as mw

    w, _ = window
    doc_id = w._doc_id
    obj_id = w.controller.document(doc_id).iter_outline_ids()[0]
    svg = tmp_path / "diagram.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="2"/>\n',
        encoding="utf-8",
    )
    w.controller.require_service().set_figure(doc_id, obj_id, svg)
    w.controller.reload_document(doc_id)
    _save_to(monkeypatch, tmp_path / "with-figure.pdf")
    seen: dict = {}

    def fake_pdf(doc, **k):
        seen.update(k)
        k["unrendered"].append(obj_id)  # as if draw.io couldn't render it
        return b"%PDF-stub"

    monkeypatch.setattr(mw, "export_pdf", fake_pdf)
    monkeypatch.setattr(mw, "find_drawio", lambda: None)
    notes: list[str] = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "information",
        staticmethod(lambda _p, _t, text, *a, **k: notes.append(text) or 0),
    )

    _export_and_wait(qtbot, w)

    assert seen["folder"] == w.controller.folder(doc_id)
    assert len(notes) == 1
    assert obj_id in notes[0]
    assert "couldn't find" in notes[0]


def test_export_with_rendered_figures_shows_no_notice(
    window, tmp_path, monkeypatch, qtbot
):
    from mellon.gui import main_window as mw

    w, _ = window
    doc_id = w._doc_id
    obj_id = w.controller.document(doc_id).iter_outline_ids()[0]
    svg = tmp_path / "diagram.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="2"/>',
        encoding="utf-8",
    )
    w.controller.require_service().set_figure(doc_id, obj_id, svg)
    w.controller.reload_document(doc_id)
    _save_to(monkeypatch, tmp_path / "rendered.pdf")
    # draw.io rendered every figure: the out-list stays empty.
    monkeypatch.setattr(mw, "export_pdf", lambda doc, **k: b"%PDF-stub")
    notes: list[str] = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "information",
        staticmethod(lambda _p, _t, text, *a, **k: notes.append(text) or 0),
    )

    _export_and_wait(qtbot, w)

    assert notes == []


def test_export_without_figures_shows_no_figure_notice(
    window, tmp_path, monkeypatch, qtbot
):
    from mellon.export import unrendered_figures
    from mellon.gui import main_window as mw

    w, _ = window
    assert unrendered_figures(w.controller.document(w._doc_id)) == []
    _save_to(monkeypatch, tmp_path / "plain.pdf")
    monkeypatch.setattr(mw, "export_pdf", lambda doc, **k: b"%PDF-stub")
    notes: list[str] = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "information",
        staticmethod(lambda _p, _t, text, *a, **k: notes.append(text) or 0),
    )

    _export_and_wait(qtbot, w)

    assert notes == []
    assert (tmp_path / "plain.pdf").exists()


def test_export_write_error_is_shown_in_a_dialog(window, tmp_path, monkeypatch, qtbot):
    from mellon.gui import main_window as mw

    w, _ = window
    _save_to(monkeypatch, tmp_path / "missing-dir" / "out.pdf")
    monkeypatch.setattr(mw, "export_pdf", lambda doc, **k: b"%PDF-stub")
    boxes: list[str] = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "critical",
        staticmethod(lambda _p, _t, text, *a, **k: boxes.append(text) or 0),
    )

    _export_and_wait(qtbot, w)

    assert len(boxes) == 1 and "Could not write file" in boxes[0]
    assert w._status.currentMessage() == "Export failed."


@pytest.mark.parametrize("outcome", ["written", "tectonic_error", "crash"])
def test_export_job_outliving_its_signals_does_not_raise(
    window, tmp_path, monkeypatch, outcome
):
    """Quitting the app mid-export: Qt waits for the job, but PySide has
    already deleted its signals object. The job's last emit must not raise
    (it reached the excepthook and the log file), and the PDF still lands."""
    import shiboken6

    from mellon.export import TectonicError
    from mellon.gui import main_window as mw

    w, _ = window
    doc = w.controller.document(w._doc_id)

    def fake_pdf(doc, **k):
        if outcome == "tectonic_error":
            raise TectonicError("tectonic failed")
        if outcome == "crash":
            raise ValueError("unexpected")
        return b"%PDF-late"

    monkeypatch.setattr(mw, "export_pdf", fake_pdf)
    signals = mw._ExportSignals()
    shiboken6.delete(signals)
    target = tmp_path / "late.pdf"

    mw._ExportJob(signals, doc, w.controller.folder(w._doc_id), None, target).run()

    assert target.exists() is (outcome == "written")

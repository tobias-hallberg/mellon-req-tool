"""Unit + integration tests for outline chapter numbering.

`_chapter_numbers()` is pure, so the unit tests fabricate minimal outlines and
object dicts without touching Qt or the filesystem. The integration test drives
the full OutlineModel through the existing GUI fixture.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


REQ_TEST = (Path(__file__).resolve().parents[2] / "req-test").resolve()


def _obj(type_: str, description: str = ""):
    from mellon.schema import RequirementObject

    return RequirementObject(id="x", attrs={"type": type_}, description=description)


# --- unit tests on the numbering helper -----------------------------------


def test_chapter_numbers_top_level_headings_only():
    from mellon.gui.models.outline import _chapter_numbers
    from mellon.schema import OutlineNode

    outline = (OutlineNode("A"), OutlineNode("B"), OutlineNode("C"))
    objects = {
        "A": _obj("Heading", "Intro"),
        "B": _obj("Heading", "Body"),
        "C": _obj("Heading", "Outro"),
    }
    assert _chapter_numbers(outline, objects) == {"A": "1", "B": "2", "C": "3"}


def test_chapter_numbers_nested_headings():
    from mellon.gui.models.outline import _chapter_numbers
    from mellon.schema import OutlineNode

    outline = (
        OutlineNode("A", children=(OutlineNode("A1"), OutlineNode("A2"))),
        OutlineNode("B"),
    )
    objects = {
        "A": _obj("Heading"),
        "A1": _obj("Heading"),
        "A2": _obj("Heading"),
        "B": _obj("Heading"),
    }
    assert _chapter_numbers(outline, objects) == {
        "A": "1",
        "A1": "1.1",
        "A2": "1.2",
        "B": "2",
    }


def test_chapter_numbers_skip_non_heading_siblings():
    from mellon.gui.models.outline import _chapter_numbers
    from mellon.schema import OutlineNode

    # H1, Req, H2 at top level -> H1=1, H2=2 (Req doesn't consume a number)
    outline = (OutlineNode("H1"), OutlineNode("R"), OutlineNode("H2"))
    objects = {
        "H1": _obj("Heading"),
        "R": _obj("Functional"),
        "H2": _obj("Heading"),
    }
    numbers = _chapter_numbers(outline, objects)
    assert numbers == {"H1": "1", "H2": "2"}


def test_chapter_numbers_non_heading_children_do_not_renumber():
    from mellon.gui.models.outline import _chapter_numbers
    from mellon.schema import OutlineNode

    # Heading with Req, Heading, Req children -> only the nested heading gets a number
    outline = (
        OutlineNode(
            "H",
            children=(OutlineNode("R1"), OutlineNode("H1"), OutlineNode("R2")),
        ),
    )
    objects = {
        "H": _obj("Heading"),
        "R1": _obj("Functional"),
        "H1": _obj("Heading"),
        "R2": _obj("Functional"),
    }
    assert _chapter_numbers(outline, objects) == {"H": "1", "H1": "1.1"}


def test_chapter_numbers_missing_object_is_not_heading():
    from mellon.gui.models.outline import _chapter_numbers
    from mellon.schema import OutlineNode

    outline = (OutlineNode("A"), OutlineNode("MISSING"), OutlineNode("B"))
    objects = {"A": _obj("Heading"), "B": _obj("Heading")}
    # Missing object falls through as non-heading, doesn't crash, doesn't number.
    assert _chapter_numbers(outline, objects) == {"A": "1", "B": "2"}


def test_chapter_numbers_no_type_attr_is_not_heading():
    from mellon.gui.models.outline import _chapter_numbers
    from mellon.schema import OutlineNode, RequirementObject

    outline = (OutlineNode("X"),)
    objects = {"X": RequirementObject(id="X", attrs={}, description="no type")}
    assert _chapter_numbers(outline, objects) == {}


# --- integration via the real VCU3-SRS fixture -----------------------------


_needs_fixture = pytest.mark.skipif(
    not (REQ_TEST / ".git").exists(),
    reason="req-test fixture not available",
)


@pytest.fixture
def outline_model(qtbot, tmp_path: Path, clean_worktree):
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    dst = tmp_path / "req-test"
    shutil.copytree(REQ_TEST, dst)
    clean_worktree(dst)
    svc = DocumentService(dst, cache=True)
    docs = sorted(svc.get_all_documents())
    w = DocumentEditorWindow(docs[0], svc)
    qtbot.addWidget(w)
    w.controller.select_document("VCU3-SRS")
    return w


def _label_for(model, ref_id: str) -> str:
    """Find the label of the row whose OBJ_ID_ROLE matches ref_id."""
    from PySide6.QtCore import QModelIndex

    from mellon.gui.models.outline import OBJ_ID_ROLE

    def walk(parent):
        for row in range(model.rowCount(parent)):
            idx = model.index(row, 0, parent)
            if idx.data(OBJ_ID_ROLE) == ref_id:
                return idx.data()
            found = walk(idx)
            if found is not None:
                return found
        return None

    return walk(QModelIndex())


@_needs_fixture
def test_headings_render_with_chapter_numbers(outline_model):
    model = outline_model.outline_pane._outline_model
    # VCU3-SRS-0 is a Heading with description "Overtemperature Protection".
    assert _label_for(model, "VCU3-SRS-0") == "1  Overtemperature Protection"
    # VCU3-SRS-14 is the second top-level Heading "Power Management".
    # VCU3-SRS-10 is between them (Design Constraint, not heading) and must
    # not consume a chapter number.
    assert _label_for(model, "VCU3-SRS-14") == "2  Power Management"


@_needs_fixture
def test_non_heading_rows_keep_ref_id_prefix(outline_model):
    model = outline_model.outline_pane._outline_model

    label = _label_for(model, "VCU3-SRS-10")
    assert label is not None and label.startswith("VCU3-SRS-10")

    label1 = _label_for(model, "VCU3-SRS-1")
    assert label1 is not None and label1.startswith("VCU3-SRS-1")

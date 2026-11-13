"""Tests for the Project Explorer's merge banner and the Resolve merge dialog."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from mellon import (
    create_object,
    delete_object,
    load_document,
    set_attribute,
    set_description,
)
from tests.test_merge import (  # noqa: F401 — `repo` is a fixture
    OBJ1,
    SRS,
    _both_edit,
    _branch,
    _clash,
    _commit,
    _conflicts,
    _merge,
    repo,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def explorer(qtbot):
    from mellon.gui.project_explorer_window import ProjectExplorerWindow

    w = ProjectExplorerWindow()
    qtbot.addWidget(w)
    return w


def _dialog(qtbot, repo: Path):
    from mellon import merge
    from mellon.gui.dialogs.merge_dialog import MergeDialog

    dlg = MergeDialog(repo, merge.plan(repo))
    qtbot.addWidget(dlg)
    return dlg


def _texts(dlg) -> str:
    return "\n".join(label.text() for label in dlg.findChildren(QLabel))


def _clash_and_both_edited(repo: Path) -> None:
    """VCU3-SRS-34 created on both branches (b later), and VCU3-SRS-1's text
    and status changed on both."""
    srs = repo / SRS
    _branch(repo, "a")
    a1 = create_object(srs, parent_id="VCU3-SRS-14", description="A first")
    set_attribute(srs, a1, "type", "Requirement")
    set_description(srs, "VCU3-SRS-1", "Mine.")
    set_attribute(srs, "VCU3-SRS-1", "status", "Approved")
    _commit(repo, "a", when=1_700_000_000)
    _branch(repo, "b")
    b1 = create_object(srs, parent_id="VCU3-SRS-19", description="B only")
    set_attribute(srs, b1, "type", "Requirement")
    set_description(srs, "VCU3-SRS-1", "Theirs.")
    set_attribute(srs, "VCU3-SRS-1", "status", "Reviewed")
    _commit(repo, "b", when=1_700_000_100)
    _merge(repo, "a", "b")


def test_banner_follows_the_merge(explorer, repo: Path):
    explorer.open_repo(repo)
    assert explorer.merge_banner.isHidden()

    _clash(repo)
    explorer._refresh_documents()  # F5
    assert not explorer.merge_banner.isHidden()
    assert "file(s) conflict" in explorer.merge_banner._label.text()
    assert explorer.merge_banner._button.isVisibleTo(explorer.merge_banner)

    from mellon import merge

    merge.resolve(repo)
    explorer._refresh_documents()
    assert "No conflicts left" in explorer.merge_banner._label.text()
    assert not explorer.merge_banner._button.isVisibleTo(explorer.merge_banner)


def test_dialog_renumbers_without_asking_and_merges_by_hand(qtbot, repo: Path):
    _clash_and_both_edited(repo)
    dlg = _dialog(qtbot, repo)
    shown = _texts(dlg)
    # The clash is a line, not a choice; Resolve works at once.
    assert "VCU3-SRS-34 on b was created later" in shown and "VCU3-SRS-35" in shown
    assert dlg._resolve_btn.isEnabled()
    # The requirement's block shows what it traces to.
    syrs42 = load_document(repo / "VCU3-SyRS").objects["VCU3-SyRS-42"].description
    assert "satisfies VCU3-SyRS-42: " + " ".join(syrs42.split()) in shown
    # Defaults: mine.
    assert dlg.edits() == {OBJ1: {"description": "Mine.", "attr:status": "ours"}}

    # "Use theirs" fills the result; then edit it and take their status.
    text = dlg._fields[OBJ1]["description"]
    [use_theirs] = [b for b in dlg.findChildren(QPushButton) if b.text() == "Use theirs"]
    use_theirs.click()
    assert text.toPlainText() == "Theirs."
    text.setPlainText("Mine and theirs.")
    dlg._fields[OBJ1]["attr:status"].setCurrentIndex(1)
    dlg._resolve()

    assert dlg.result() == dlg.DialogCode.Accepted
    assert _conflicts(repo) == []
    doc = load_document(repo / SRS)
    assert doc.objects["VCU3-SRS-1"].description == "Mine and theirs."
    assert doc.objects["VCU3-SRS-1"].attrs["status"] == "Reviewed"
    assert doc.objects["VCU3-SRS-34"].description == "A first"
    assert doc.objects["VCU3-SRS-35"].description == "B only"


def test_dialog_keep_checkbox_for_a_deleted_object(qtbot, repo: Path):
    _both_edit(
        repo,
        lambda r: set_description(r / SRS, "VCU3-SRS-10", "Edited on a."),
        lambda r: delete_object(r / SRS, "VCU3-SRS-10", force=True),
    )
    dlg = _dialog(qtbot, repo)
    [(path, keep)] = dlg._keeps.items()
    assert keep.isChecked() and "deleted on b" in keep.text()
    keep.setChecked(False)
    assert dlg.delete() == {path}
    dlg._resolve()
    assert "VCU3-SRS-10" not in load_document(repo / SRS).objects


def test_explorer_resolve_refreshes_after_the_dialog(explorer, repo: Path, monkeypatch):
    from mellon.gui import project_explorer_window as pew

    _clash(repo)
    explorer.open_repo(repo)
    assert not explorer.merge_banner.isHidden()

    def accept(dlg):
        dlg._resolve()
        return dlg.result()

    monkeypatch.setattr(pew.MergeDialog, "exec", accept)
    explorer._resolve_merge()

    assert _conflicts(repo) == []
    assert "No conflicts left" in explorer.merge_banner._label.text()
    assert "Make the merge commit" in explorer._status.currentMessage()
    # The renumbered object loads with the rest.
    assert "VCU3-SRS-36" in explorer.controller.document(SRS).objects


# MELLON-VER-SRS blind tests
import re  # noqa: E402

from lxml import etree  # noqa: E402
from PySide6.QtWidgets import QAbstractButton, QWidget  # noqa: E402

from tests.edit_b_support import _plain  # noqa: E402
from tests.store_support import DECL  # noqa: E402
from tests.ver_support import (  # noqa: E402
    ATTRIBUTES,
    SVG_BLUE,
    SVG_RED,
    _action,
    _activate,
    _branch as ver_branch,
    _button,
    _commit as ver_commit,
    _git,
    _make_doc_repo,
    _make_repo,
    _manifest,
    _merge_dialog,
    _merge_stops,
    _obj,
    _open_explorer,
    _start_clean_merge,
    _texts as ver_texts,
    _unmerged,
    _write,
    _xml,
)


SVG_GREEN = SVG_RED.replace("red", "green")


def _table(cell: str) -> str:
    return (
        "  <table>\n"
        "    <columns>\n"
        '      <col align="left"/>\n'
        "    </columns>\n"
        "    <row>\n"
        f"      <cell>{cell}</cell>\n"
        "    </row>\n"
        "  </table>\n"
    )


def _canon(data: bytes | str) -> str:
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    data = re.sub(r"^<\?xml[^>]*\?>\s*", "", data)
    return etree.canonicalize(xml_data=data, strip_text=True)


# ==========================================================================
# A merge in progress
# ==========================================================================


def _conflicting_merge(root: Path, *, more: bool = False) -> None:
    """main and side each change CHILD-0 (and with `more`, PARENT-1 and
    NOTES.txt); merging side stops on the conflicts."""
    if more:
        _write(root / "NOTES.txt", "base\n")
        ver_commit(root, "notes", tick=2)
    _git(root, "checkout", "-q", "-b", "side")
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Theirs."))
    if more:
        _write(root / "PARENT/objects/PARENT-1.xml", _obj("PARENT-1", "Theirs."))
        _write(root / "NOTES.txt", "theirs\n")
    ver_commit(root, "side", tick=3)
    _git(root, "checkout", "-q", "main")
    _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Mine."))
    if more:
        _write(root / "PARENT/objects/PARENT-1.xml", _obj("PARENT-1", "Mine."))
        _write(root / "NOTES.txt", "ours\n")
    ver_commit(root, "main", tick=4)
    _git(root, "merge", "side", check=False)
    assert (root / ".git/MERGE_HEAD").exists(), "the merge didn't stop"


def test_ver_srs_297_project_explorer_checks_for_a_merge_when_activated(tmp_path, qtbot):
    """MELLON-VER-SRS-297: The Project Explorer checks for a merge in progress
    when it is activated: the banner shows without pressing F5."""
    root = _make_repo(tmp_path / "repo")
    pe = _open_explorer(qtbot, root)
    assert not pe.merge_banner.isVisibleTo(pe), "the merge banner shows with no merge"
    _conflicting_merge(root)

    _activate(qtbot, pe)

    assert pe.merge_banner.isVisibleTo(pe), "activating the window didn't show the merge banner"


def test_ver_srs_297_project_explorer_hides_the_banner_when_activated_after_an_abort(tmp_path, qtbot):
    """MELLON-VER-SRS-297 (with -296): after the merge is aborted in a git
    client, activating the Project Explorer finds no merge and the banner goes."""
    root = _make_repo(tmp_path / "repo")
    _conflicting_merge(root)
    pe = _open_explorer(qtbot, root)
    assert pe.merge_banner.isVisibleTo(pe), "setup: the merge banner doesn't show"
    _git(root, "merge", "--abort")

    _activate(qtbot, pe)

    assert not pe.merge_banner.isVisibleTo(pe), "the merge banner stays after the merge was aborted"


def test_ver_srs_300_merge_banner_shows_the_number_of_conflicting_files(tmp_path, qtbot):
    """MELLON-VER-SRS-300: While a merge is in progress with conflicting files,
    the merge banner shows their number: two document files and NOTES.txt make 3."""
    root = _make_repo(tmp_path / "repo")
    _conflicting_merge(root, more=True)
    assert len(_unmerged(root)) == 3
    pe = _open_explorer(qtbot, root)

    text = " ".join(ver_texts(pe.merge_banner))

    assert pe.merge_banner.isVisibleTo(pe)
    assert re.search(r"(?<!\d)3(?!\d)", text), f"the banner doesn't show 3: {text}"


@pytest.mark.parametrize("how", ["no_commit", "resolved"])
def test_ver_srs_303_merge_banner_says_to_commit_in_a_git_client_without_conflicts(tmp_path, qtbot, how):
    """MELLON-VER-SRS-303: While a merge is in progress with no conflicting
    file (stopped by --no-commit, or with its conflicts marked resolved), the
    merge banner says to make the merge commit in a git client, and offers no
    Resolve merge… action."""
    root = _make_repo(tmp_path / "repo")
    if how == "no_commit":
        _start_clean_merge(root)
    else:
        _conflicting_merge(root)
        _write(root / "CHILD/objects/CHILD-0.xml", _obj("CHILD-0", "Resolved."))
        _git(root, "add", "CHILD/objects/CHILD-0.xml")
    assert _unmerged(root) == []
    pe = _open_explorer(qtbot, root)
    _action(pe, "Refresh").trigger()
    qtbot.wait(20)

    banner = pe.merge_banner
    text = " ".join(t for t in ver_texts(banner)).lower()

    assert banner.isVisibleTo(pe), "the merge banner doesn't show"
    assert "git client" in text and "commit" in text, f"the banner doesn't say to commit in a git client: {text}"
    resolve = [b for b in banner.findChildren(QAbstractButton) if "resolve" in b.text().lower() and b.isVisibleTo(banner)]
    assert resolve == [], "the banner offers Resolve merge…"


def _conflict_pair(root: Path, rel: str, ours: str | bytes, theirs: str | bytes) -> None:
    ver_branch(root, "side")
    _write(root / rel, theirs)
    ver_commit(root, "theirs", tick=3)
    _git(root, "checkout", "-q", "main")
    _write(root / rel, ours)
    ver_commit(root, "ours", tick=4)


# ==========================================================================
# What the user merges: the Resolve merge dialog
# ==========================================================================


def _side_choices(dlg: QWidget) -> tuple[list[QAbstractButton], list[QAbstractButton]]:
    """The dialog's Keep mine / Keep theirs choices, one pair per item."""
    buttons = [b for b in dlg.findChildren(QAbstractButton) if b.isCheckable() and b.isVisibleTo(dlg)]
    mine = [b for b in buttons if _plain(b.text()).lower().startswith("keep mine")]
    theirs = [b for b in buttons if _plain(b.text()).lower().startswith("keep theirs")]
    return mine, theirs


def test_ver_srs_326_merge_dialog_lists_a_conflicting_file_outside_document_folders(tmp_path, qtbot):
    """MELLON-VER-SRS-326: The Resolve merge dialog lists each conflicting file
    outside the document folders: NOTES.txt."""
    root = _make_repo(tmp_path / "repo")
    _conflicting_merge(root, more=True)
    assert "NOTES.txt" in _unmerged(root)

    dlg = _merge_dialog(qtbot, root)

    assert any("NOTES.txt" in t for t in ver_texts(dlg)), ver_texts(dlg)


def test_ver_srs_345_merge_dialog_asks_for_the_merged_table_and_figure(tmp_path, qtbot):
    """MELLON-VER-SRS-345: When both sides changed an object's table, or its
    figure, to different values, the Resolve merge dialog asks the user for
    the merged value: a choice for each, which the resolution follows."""
    objects = {"DOC-1": _obj("DOC-1", "One.", table=_table("Base cell"))}
    root = _make_doc_repo(tmp_path / "repo", objects, extra={"DOC/figures/DOC-1.svg": SVG_RED})
    ver_branch(root, "side")
    _write(root / "DOC/objects/DOC-1.xml", _obj("DOC-1", "One.", table=_table("Theirs cell")))
    _write(root / "DOC/figures/DOC-1.svg", SVG_BLUE)
    ver_commit(root, "theirs", tick=3)
    _git(root, "checkout", "-q", "main")
    _write(root / "DOC/objects/DOC-1.xml", _obj("DOC-1", "One.", table=_table("Ours cell")))
    _write(root / "DOC/figures/DOC-1.svg", SVG_GREEN)
    ver_commit(root, "ours", tick=4)
    _merge_stops(root, "side", "DOC/objects/DOC-1.xml", "DOC/figures/DOC-1.svg")

    dlg = _merge_dialog(qtbot, root)
    mine, theirs = _side_choices(dlg)
    texts = " ".join(ver_texts(dlg)).lower()

    assert "table" in texts and "figure" in texts, f"the dialog doesn't name the table and figure: {texts}"
    assert (len(mine), len(theirs)) == (2, 2), f"the dialog doesn't ask for the table and the figure: {ver_texts(dlg)}"
    for button in theirs:
        button.click()
    _button(dlg, "Resolve").click()
    qtbot.wait(20)
    assert _unmerged(root) == []
    assert [c.text for c in _xml((root / "DOC/objects/DOC-1.xml").read_bytes()).iter("cell")] == ["Theirs cell"]
    assert (root / "DOC/figures/DOC-1.svg").read_bytes() == SVG_BLUE.encode(), "the answer for the figure wasn't used"


def test_ver_srs_346_merge_dialog_shows_ours_and_theirs_texts(tmp_path, qtbot):
    """MELLON-VER-SRS-346: When both sides changed a description, or metadata
    text, the Resolve merge dialog shows ours and theirs."""
    root = _make_doc_repo(tmp_path / "repo")
    ver_branch(root, "side")
    _write(root / "DOC/objects/DOC-1.xml", _obj("DOC-1", "Theirs description."))
    _write(root / "DOC/DOC.xml", _manifest("DOC", ["DOC-1", "DOC-2", "DOC-3"], title="Theirs title"))
    ver_commit(root, "theirs", tick=3)
    _git(root, "checkout", "-q", "main")
    _write(root / "DOC/objects/DOC-1.xml", _obj("DOC-1", "Ours description."))
    _write(root / "DOC/DOC.xml", _manifest("DOC", ["DOC-1", "DOC-2", "DOC-3"], title="Ours title"))
    ver_commit(root, "ours", tick=4)
    _merge_stops(root, "side", "DOC/objects/DOC-1.xml", "DOC/DOC.xml")

    text = "\n".join(ver_texts(_merge_dialog(qtbot, root)))

    for wanted in ("Ours description.", "Theirs description.", "Ours title", "Theirs title"):
        assert wanted in text, f"the dialog doesn't show {wanted!r}"


VIEW = DECL + '<view name="Main">\n{attrs}</view>\n'


FILTER = DECL + '<filter name="Drafts" combinator="all" keep-ancestors="true">\n  <rule kind="attr" op="{op}" attr="status" value="{value}"/>\n</filter>\n'


RULES = DECL + '<rules>\n  <rule>\n    <when type="Heading"/>\n    <then><attr name="{name}" status="forbidden"/></then>\n  </rule>\n</rules>\n'


def _view(*names):
    return VIEW.format(attrs="".join(f'  <attribute name="{n}"/>\n' for n in names))


def _priority_attrs(*extra_options: str, owner: bool = False) -> str:
    opts = "".join(f"      <option>{o}</option>\n" for o in ("High", "Low", *extra_options))
    base = ATTRIBUTES.replace(
        '    <attribute name="priority" kind="enum" required="false">\n      <option>High</option>\n      <option>Low</option>\n    </attribute>\n',
        f'    <attribute name="priority" kind="enum" required="false">\n{opts}    </attribute>\n',
    )
    if owner:
        base += '    <attribute name="owner" kind="text" required="false"/>\n'
    return base


def _whole_items_repo(root: Path) -> dict[str, dict[str, str]]:
    """Both sides change DOC-1's table and figure, a view, a filter, the
    rules file and the priority definition; theirs also adds `owner`."""
    versions = {
        "ours": dict(cell="Ours cell", svg=SVG_GREEN, view=("status", "priority"), op="eq", value="Approved",
                     rule="priority", options=("Medium",), owner=False),
        "theirs": dict(cell="Theirs cell", svg=SVG_BLUE, view=("priority",), op="ne", value="Draft",
                       rule="status", options=("Urgent",), owner=True),
    }
    objects = {"DOC-1": _obj("DOC-1", "One.", table=_table("Base cell"))}
    extra = {
        "DOC/figures/DOC-1.svg": SVG_RED,
        "DOC/views/main.xml": _view("status"),
        "DOC/filters/drafts.xml": FILTER.format(op="eq", value="Draft"),
        "DOC/DOC.rules.xml": RULES.format(name="rationale"),
    }
    _make_doc_repo(root, objects, extra=extra)
    files = {}

    def apply(side):
        v = versions[side]
        out = {
            "DOC/objects/DOC-1.xml": _obj("DOC-1", "One.", table=_table(v["cell"])),
            "DOC/figures/DOC-1.svg": v["svg"],
            "DOC/views/main.xml": _view(*v["view"]),
            "DOC/filters/drafts.xml": FILTER.format(op=v["op"], value=v["value"]),
            "DOC/DOC.rules.xml": RULES.format(name=v["rule"]),
            "DOC/DOC.xml": _manifest("DOC", ["DOC-1"], title="Base title",
                                     attributes=_priority_attrs(*v["options"], owner=v["owner"])),
        }
        for rel, text in out.items():
            _write(root / rel, text)
        files[side] = out

    ver_branch(root, "side")
    apply("theirs")
    ver_commit(root, "theirs", tick=3)
    _git(root, "checkout", "-q", "main")
    apply("ours")
    ver_commit(root, "ours", tick=4)
    _merge_stops(root, "side", *files["ours"])
    return files


def test_ver_srs_349_merge_dialog_resolves_whole_items_to_ours_by_default(tmp_path, qtbot, ui):
    """MELLON-VER-SRS-349: When both sides changed a table, figure, view,
    filter, rules file or attribute definition, the Resolve merge dialog
    offers ours or theirs with ours chosen: resolving without input gives
    ours' version of each, and theirs' new attribute definition stays."""
    root = tmp_path / "repo"
    files = _whole_items_repo(root)
    dlg = _merge_dialog(qtbot, root)

    _button(dlg, "Resolve").click()
    qtbot.wait(20)

    assert _unmerged(root) == [], f"the merge isn't resolved; messages: {ui.message_text()}"
    ours = files["ours"]
    obj = _xml((root / "DOC/objects/DOC-1.xml").read_bytes())
    assert [c.text for c in obj.iter("cell")] == ["Ours cell"], "the table isn't ours'"
    assert (root / "DOC/figures/DOC-1.svg").read_bytes() == ours["DOC/figures/DOC-1.svg"].encode(), "figure"
    for rel in ("DOC/views/main.xml", "DOC/filters/drafts.xml", "DOC/DOC.rules.xml"):
        assert _canon((root / rel).read_bytes()) == _canon(ours[rel]), f"{rel} isn't ours'"
    man = _xml((root / "DOC/DOC.xml").read_bytes())
    defs = {a.get("name"): [o.text for o in a.iter("option")] for a in man.iter("attribute")}
    assert defs.get("priority") == ["High", "Low", "Medium"], f"the priority definition isn't ours': {defs}"
    assert "owner" in defs, "theirs' new attribute definition was dropped"


def test_ver_srs_349_merge_dialog_offers_ours_or_theirs_for_each_whole_item(tmp_path, qtbot):
    """MELLON-VER-SRS-349: the dialog offers a choice of ours and theirs, with
    ours chosen, for each of the table, figure, view, filter, rules file and
    attribute definition."""
    root = tmp_path / "repo"
    _whole_items_repo(root)
    dlg = _merge_dialog(qtbot, root)

    mine, theirs = _side_choices(dlg)

    assert (len(mine), len(theirs)) == (6, 6), f"not one choice per item: {ver_texts(dlg)}"
    assert all(b.isChecked() for b in mine), "ours isn't chosen for every item"
    assert not any(b.isChecked() for b in theirs), "theirs is chosen for an item"


def test_ver_srs_353_merge_dialog_lists_fields_merged_in_from_theirs(tmp_path, qtbot):
    """MELLON-VER-SRS-353: The Resolve merge dialog lists, with each object
    merged by hand, the fields Mellon took from theirs without a conflict:
    priority, beside the conflicting description."""
    root = _make_doc_repo(tmp_path / "repo")
    _conflict_pair(root, "DOC/objects/DOC-1.xml", _obj("DOC-1", "Ours."), _obj("DOC-1", "Theirs.", priority="High"))
    _merge_stops(root, "side", "DOC/objects/DOC-1.xml")

    lines = [t for t in ver_texts(_merge_dialog(qtbot, root)) if "merged in" in t.lower()]

    assert lines, "the dialog lists nothing as merged in"
    assert any("priority" in t.lower() for t in lines), f"priority isn't listed: {lines}"

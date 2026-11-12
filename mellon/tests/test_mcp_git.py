"""Tests for the MCP git/review tools (Slice M3).

`req_test_copy` in conftest could be reused, but it currently lives in
test_mcp_write.py. Tests here re-declare the fixture so the two files stay
independent — copies of req-test/ are cheap.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pygit2
import pytest
from lxml import etree

import mellon.mcp.server as mcp_server
from mellon import diff, editor, git_ops
from mellon.diff import LATEST
from mellon.mcp import server
from tests.compare_support import (
    DOC,
    PARTS,
    GitRepo,
    _ambiguous_prefix,
    _build,
    _build_dirty_repo,
    _changed,
    _everything,
    _r2_commit,
    _relinked_repo,
    _rewrite,
    _shows_hash_of,
    _snapshot,
    build_other_only,
    build_snapshot_repo,
    build_spec,
    links_xml,
    snapshot,
    snapshot_difference,
)


@pytest.fixture
def req_test_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_worktree) -> Path:
    src = (Path(__file__).resolve().parents[2] / "req-test").resolve()
    if not (src / ".git").exists():
        pytest.skip(f"req-test repo not available at {src}")
    dst = tmp_path / "req-test"
    shutil.copytree(src, dst)
    clean_worktree(dst)
    monkeypatch.setattr(server._State, "repo_root", dst.resolve())
    monkeypatch.setattr(server._State, "read_only", False)
    return dst


# --- commit_changes -----------------------------------------------------


def test_commit_changes_round_trip(req_test_copy: Path) -> None:
    # Make a small edit via the MCP edit tool, then commit.
    edit = server.edit_description(
        "VCU3-SRS", "VCU3-SRS-2",
        "The VCU shall sample the coolant temperature at a rate of at least 25 Hz.",
    )
    assert edit["ok"]

    result = server.commit_changes("Bump coolant sample rate to 25 Hz")
    assert result["ok"]
    assert len(result["short_hash"]) == 7
    assert "Via: mellon-mcp" in result["commit_message"]

    # HEAD advanced; message contains both the summary and the trailer.
    repo = pygit2.Repository(str(req_test_copy))
    head_commit = repo.head.peel(pygit2.Commit)
    assert str(head_commit.id).startswith(result["short_hash"])
    assert "Bump coolant sample rate to 25 Hz" in head_commit.message
    assert "Via: mellon-mcp" in head_commit.message


def test_commit_changes_can_suppress_trailer(req_test_copy: Path) -> None:
    server.edit_description(
        "VCU3-SRS", "VCU3-SRS-2", "Rate change without trailer."
    )
    result = server.commit_changes(
        "Rate change", include_trailer=False
    )
    assert result["ok"]
    assert "Via: mellon-mcp" not in result["commit_message"]


def test_commit_changes_without_paths_commits_only_document_folders(
    req_test_copy: Path,
) -> None:
    server.edit_description("VCU3-SRS", "VCU3-SRS-2", "Scoped commit.")
    (req_test_copy / "secrets.env").write_text("TOKEN=x\n", encoding="utf-8")
    (req_test_copy / "exports").mkdir()
    (req_test_copy / "exports" / "out.txt").write_text("export\n", encoding="utf-8")

    result = server.commit_changes("Scoped")
    assert result["ok"]

    repo = pygit2.Repository(str(req_test_copy))
    tree = repo.head.peel(pygit2.Commit).tree
    assert "Scoped commit." in repo[
        tree["VCU3-SRS/objects/VCU3-SRS-2.xml"].id
    ].data.decode()
    assert "secrets.env" not in tree
    assert "exports" not in tree


def test_commit_changes_nothing_staged(req_test_copy: Path) -> None:
    result = server.commit_changes("nothing")
    assert result["ok"] is False
    assert result["error_type"] == "GitError"


def test_commit_changes_respects_paths(req_test_copy: Path) -> None:
    # Two edits, but only commit one path.
    server.edit_description("VCU3-SRS", "VCU3-SRS-1", "Committed via paths.")
    server.edit_description("VCU3-SRS", "VCU3-SRS-2", "NOT committed this round.")

    result = server.commit_changes(
        "Partial commit",
        paths=["VCU3-SRS/objects/VCU3-SRS-1.xml"],
    )
    assert result["ok"]

    repo = pygit2.Repository(str(req_test_copy))
    tree = repo.head.peel(pygit2.Commit).tree
    assert "Committed via paths." in repo[
        tree["VCU3-SRS/objects/VCU3-SRS-1.xml"].id
    ].data.decode()
    # SRS-2 was not staged, so in the committed tree it still has the
    # original description.
    assert "NOT committed" not in repo[
        tree["VCU3-SRS/objects/VCU3-SRS-2.xml"].id
    ].data.decode()


# --- git_history --------------------------------------------------------


def test_git_history_matches_fixture(req_test_copy: Path) -> None:
    hist = server.git_history("VCU3-SyRS", "VCU3-SyRS-42")
    # Fixture history: added in "Added SyRS.", fixed in "Fixed VCU3-SyRS-42",
    # tightened in "Tighten SyRS-42 to 300ms", then the type schema migration
    # and the req-type addition. Order: newest first.
    assert [e["message"] for e in hist] == [
        "Add req-type attribute to VCU3-SyRS and VCU3-SRS",
        "Migrate type schema: replace domain types with Requirement",
        "Tighten SyRS-42 to 300ms",
        "Fixed VCU3-SyRS-42",
        "Added SyRS.",
    ]


def test_git_history_unknown_object_raises(req_test_copy: Path) -> None:
    with pytest.raises(ValueError, match="No object 'VCU3-SyRS-999'"):
        server.git_history("VCU3-SyRS", "VCU3-SyRS-999")


# --- show_object_at -----------------------------------------------------


def test_show_object_at_returns_historical_description(
    req_test_copy: Path, fixture_commit
) -> None:
    # VCU3-SyRS-47 was edited in "Tighten VCU3-SyRS-47". The commit right
    # before it must still have the original text. Find its parent.
    repo = pygit2.Repository(str(req_test_copy))
    tight = fixture_commit(repo, "Tighten VCU3-SyRS-47")
    parent_ref = str(tight.parents[0].id)[:7]

    old = server.show_object_at("VCU3-SyRS", "VCU3-SyRS-47", parent_ref)
    assert (
        old["description"]
        == "The vehicle shall detect a supply voltage below 9 V on the 12 V rail and annunciate it to the driver."
    )
    assert old["commit"] == parent_ref

    # HEAD version has the tightened text.
    new = server.show_object_at("VCU3-SyRS", "VCU3-SyRS-47", "HEAD")
    assert "50 ms detection budget" in new["description"]


# --- review_suspect_link -----------------------------------------------


def test_review_parent_edited_suspect(
    req_test_copy: Path, fixture_commit
) -> None:
    r = server.review_suspect_link(
        "VCU3-SRS", "VCU3-SRS-15", "VCU3-SyRS", "VCU3-SyRS-47"
    )
    repo = pygit2.Repository(str(req_test_copy))
    acked = fixture_commit(repo, "Expand VCU3-SyRS with 19 objects")
    current = fixture_commit(repo, "Add req-type attribute to VCU3-SyRS and VCU3-SRS")
    assert r["state"] == "suspect"
    assert r["acked_at_commit"] == str(acked.id)[:7]
    assert r["current_commit"] == str(current.id)[:7]
    # The description before the tightening commit.
    assert r["parent_text_at_ack"] is not None
    assert "50 ms" not in r["parent_text_at_ack"]
    # The description now.
    assert "50 ms detection budget" in r["parent_text_now"]
    # Three commits touched the parent since the ack (newest first).
    assert len(r["commits_between"]) == 3
    assert "req-type" in r["commits_between"][0]["message"]
    assert "Migrate" in r["commits_between"][1]["message"]
    assert "Tighten" in r["commits_between"][2]["message"]


def test_review_never_reviewed_suspect(req_test_copy: Path) -> None:
    # SRS-17 -> SyRS-61 has no ack at creation time.
    r = server.review_suspect_link(
        "VCU3-SRS", "VCU3-SRS-17", "VCU3-SyRS", "VCU3-SyRS-61"
    )
    assert r["state"] == "suspect"
    assert r["reason"] == "never reviewed"
    assert r["acked_at_commit"] is None
    # We know the target IS in git (it was committed alongside the link).
    assert r["current_commit"] is not None
    # parent_text_now reflects disk; parent_text_at_ack is unrecoverable.
    assert "event log" in r["parent_text_now"].lower()
    assert r["parent_text_at_ack"] is None
    assert r["commits_between"] == []


def test_review_ok_link_is_clean(req_test_copy: Path) -> None:
    # SRS-1 -> SyRS-42 is the original link that was re-acked in the fixture's
    # git history; expected to be OK.
    r = server.review_suspect_link(
        "VCU3-SRS", "VCU3-SRS-1", "VCU3-SyRS", "VCU3-SyRS-42"
    )
    assert r["state"] == "ok"
    assert r["acked_at_commit"] == r["current_commit"]
    assert r["parent_text_at_ack"] == r["parent_text_now"]
    assert r["commits_between"] == []


def _commit_figure(req_test_copy: Path, doc_id: str, obj_id: str) -> None:
    svg = req_test_copy.parent / "fig.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    result = server.set_figure(doc_id, obj_id, str(svg), commit_message="Draw figure")
    assert result["ok"] and "commit" in result


def test_review_lists_figure_only_revision(req_test_copy: Path) -> None:
    """A figure-only revision makes the link suspect; the review must show
    the commit that did it, even though the parent text didn't change."""
    _commit_figure(req_test_copy, "VCU3-SyRS", "VCU3-SyRS-42")
    r = server.review_suspect_link(
        "VCU3-SRS", "VCU3-SRS-1", "VCU3-SyRS", "VCU3-SyRS-42"
    )
    assert r["state"] == "suspect"
    assert r["parent_text_at_ack"] == r["parent_text_now"]
    assert [c["message"] for c in r["commits_between"]] == ["Draw figure"]


def _review_syrs_42() -> dict:
    """Review the fixture's OK link SRS-1 -> SyRS-42."""
    return server.review_suspect_link(
        "VCU3-SRS", "VCU3-SRS-1", "VCU3-SyRS", "VCU3-SyRS-42"
    )


def test_review_ok_link_has_no_changed_fields(req_test_copy: Path) -> None:
    r = _review_syrs_42()
    assert r["changed_fields"] == []
    assert r["ack_found"] is True
    assert r["parent_uncommitted"] is False
    assert r["commits_between_truncated"] is False


def test_review_names_an_attribute_only_revision(req_test_copy: Path) -> None:
    """The description is the same before and after, so the attribute change
    has to be visible some other way or it gets acked blind."""
    result = server.set_attribute(
        "VCU3-SyRS", "VCU3-SyRS-42", "status", "Draft", commit_message="Back to draft"
    )
    assert result["ok"] and "commit" in result

    r = _review_syrs_42()

    assert r["state"] == "suspect"
    assert r["parent_text_at_ack"] == r["parent_text_now"]
    assert r["changed_fields"] == ["attr:status"]
    assert r["parent_attrs_at_ack"]["status"] == "Approved"
    assert r["parent_attrs_now"]["status"] == "Draft"


def test_review_names_a_description_revision(req_test_copy: Path) -> None:
    result = server.edit_description(
        "VCU3-SyRS", "VCU3-SyRS-42", "Within 200 ms.", commit_message="Tighten"
    )
    assert result["ok"] and "commit" in result
    assert _review_syrs_42()["changed_fields"] == ["description"]


def test_review_names_a_table_only_revision(req_test_copy: Path) -> None:
    table = {"columns": [{"align": "left"}], "rows": [{"cells": ["Signal"]}]}
    result = server.set_table(
        "VCU3-SyRS", "VCU3-SyRS-42", table, commit_message="Add table"
    )
    assert result["ok"] and "commit" in result

    r = _review_syrs_42()

    assert r["parent_text_at_ack"] == r["parent_text_now"]
    assert r["changed_fields"] == ["table"]


def test_review_names_a_figure_only_revision(req_test_copy: Path) -> None:
    _commit_figure(req_test_copy, "VCU3-SyRS", "VCU3-SyRS-42")
    assert _review_syrs_42()["changed_fields"] == ["figure"]


def test_review_flags_parent_edits_an_ack_would_not_cover(req_test_copy: Path) -> None:
    """`parent_text_now` reads the work tree, but an ack pins HEAD's version."""
    server.edit_description("VCU3-SyRS", "VCU3-SyRS-42", "Not committed yet.")

    r = _review_syrs_42()

    assert r["parent_uncommitted"] is True
    assert r["parent_text_now"] == "Not committed yet."
    # Nothing committed since the ack, so an ack would accept no change.
    assert r["changed_fields"] == []


def test_review_never_reviewed_has_no_changed_fields(req_test_copy: Path) -> None:
    r = server.review_suspect_link(
        "VCU3-SRS", "VCU3-SRS-17", "VCU3-SyRS", "VCU3-SyRS-61"
    )
    assert r["changed_fields"] is None
    assert r["ack_found"] is False
    assert r["parent_attrs_at_ack"] is None
    assert r["parent_attrs_now"]["type"] == "Requirement"


def _set_ack(req_test_copy: Path, ack: str) -> None:
    links = req_test_copy / "VCU3-SRS" / "links" / "VCU3-SRS-1.xml"
    tree = etree.parse(str(links))
    for link in tree.getroot().findall("link"):
        if link.get("target-id") == "VCU3-SyRS-42":
            link.set("ack", ack)
    tree.write(str(links), encoding="UTF-8", xml_declaration=True)


def test_review_says_when_the_ack_cannot_be_resolved(req_test_copy: Path) -> None:
    """After a rebase and gc the acked commit is gone. Say so, rather than
    returning empty results that look like nothing changed."""
    _set_ack(req_test_copy, "0123abc")

    r = _review_syrs_42()

    assert r["state"] == "suspect"
    assert r["ack_found"] is False
    assert r["changed_fields"] is None
    assert r["commits_between"] == []


def test_review_flags_a_truncated_commit_list(req_test_copy: Path) -> None:
    for n in range(21):
        result = server.edit_description(
            "VCU3-SyRS", "VCU3-SyRS-42", f"Revision {n}.", commit_message=f"Rev {n}"
        )
        assert result["ok"] and "commit" in result

    r = _review_syrs_42()

    assert len(r["commits_between"]) == 20
    assert r["commits_between_truncated"] is True


def test_git_history_includes_figure_commits(req_test_copy: Path) -> None:
    _commit_figure(req_test_copy, "VCU3-SyRS", "VCU3-SyRS-42")
    hist = server.git_history("VCU3-SyRS", "VCU3-SyRS-42")
    assert hist[0]["message"] == "Draw figure"
    inspected = server.inspect_object(
        "VCU3-SyRS", "VCU3-SyRS-42", include_history=True
    )
    assert inspected["history"][0]["message"] == "Draw figure"


def test_review_unknown_link_raises(req_test_copy: Path) -> None:
    with pytest.raises(ValueError, match="No link from"):
        server.review_suspect_link(
            "VCU3-SRS", "VCU3-SRS-1", "VCU3-SyRS", "VCU3-SyRS-999"
        )


# --- main() root guard ---------------------------------------------------


def test_main_refuses_root_outside_git(tmp_path: Path) -> None:
    assert server.main(["--repo-root", str(tmp_path)]) == 2


def test_main_refuses_root_below_git_root(req_test_copy: Path) -> None:
    assert server.main(["--repo-root", str(req_test_copy / "VCU3-SRS")]) == 2


def test_main_refuses_unopenable_repo(tmp_path: Path) -> None:
    pygit2.init_repository(str(tmp_path)).config["core.repositoryformatversion"] = 99
    assert server.main(["--repo-root", str(tmp_path)]) == 2


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let `main()` run to completion without starting the stdio loop, and
    restore the `_State` it sets."""
    monkeypatch.setattr(server.mcp, "run", lambda *a, **kw: None)
    for attr in ("repo_root", "read_only", "service"):
        monkeypatch.setattr(server._State, attr, getattr(server._State, attr))


def test_main_read_only_builds_read_only_service(req_test_copy: Path, served) -> None:
    assert server.main(["--repo-root", str(req_test_copy), "--read-only"]) == 0
    assert server._State.service.read_only is True
    assert server._svc() is server._State.service


def test_main_repo_root_defaults_to_claude_project_dir(
    req_test_copy: Path, served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(req_test_copy))
    monkeypatch.chdir(tmp_path)
    assert server.main([]) == 0
    assert server._State.repo_root == req_test_copy.resolve()


def test_main_repo_root_defaults_to_cwd(
    req_test_copy: Path, served, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(req_test_copy)
    assert server.main([]) == 0
    assert server._State.repo_root == req_test_copy.resolve()


# --- compare_document -------------------------------------------------------


def test_compare_document_defaults_to_latest_issue_and_commit(req_test_copy: Path) -> None:
    out = server.compare_document("VCU3-SRS")
    assert out["from"]["ref"] == "VCU3-SRS-issue-1"
    assert out["to"]["ref"] == "HEAD"
    assert out["total_changes"] == len(out["changes"]) > 0
    assert out["truncated"] is False
    assert set(out["counts"]) == {"Added", "Deleted", "Modified", "Moved", "Link reviewed"}
    first = out["changes"][0]
    assert set(first) >= {"obj_id", "changes", "text"}
    # Attribute changes carry the manifest's label.
    attrs = [a for c in out["changes"] for a in c.get("attrs", [])]
    assert any(a["label"] == "Requirement Type" for a in attrs)


def test_compare_document_matches_the_dialog_engine(req_test_copy: Path) -> None:
    from mellon.diff import compare_document

    out = server.compare_document("VCU3-SRS", max_changes=1000)
    diff = compare_document(req_test_copy, "VCU3-SRS", "VCU3-SRS-issue-1", "HEAD")
    assert [c["obj_id"] for c in out["changes"]] == [c.obj_id for c in diff.changes]
    assert [c["changes"] for c in out["changes"]] == [
        [str(k) for k in c.kinds] for c in diff.changes
    ]


def test_compare_document_working_copy(req_test_copy: Path) -> None:
    assert server.edit_description("VCU3-SRS", "VCU3-SRS-2", "Uncommitted text.")["ok"]
    created = server.new_object("VCU3-SRS", description="Brand new.")
    assert created["ok"]
    out = server.compare_document("VCU3-SRS", from_ref="HEAD", to_ref="working copy")
    assert out["to"] == {"ref": "working copy", "short": "working copy", "present": True}
    by_id = {c["obj_id"]: c for c in out["changes"]}
    assert by_id["VCU3-SRS-2"]["changes"] == ["Modified"]
    assert by_id["VCU3-SRS-2"]["description"]["to"] == "Uncommitted text."
    added = [c for c in out["changes"] if c["changes"] == ["Added"]]
    assert len(added) == 1
    assert added[0]["object"]["description"] == "Brand new."
    assert added[0]["object"]["figure"] is False


def test_compare_document_returns_each_commit_in_full(req_test_copy: Path) -> None:
    # MELLON-VER-SRS-87, beside the 7-digit `short` other clients read; and
    # -9: git reads @ as HEAD, here too.
    repo = pygit2.Repository(str(req_test_copy))
    issue = repo.revparse_single("VCU3-SRS-issue-1").peel(pygit2.Commit).id
    out = server.compare_document("VCU3-SRS", to_ref="@~0")
    assert out["from"]["commit"] == str(issue)
    assert out["to"]["commit"] == str(repo.head.target)
    assert out["from"]["short"] == str(issue)[:7]


def test_compare_document_caps_the_list(req_test_copy: Path) -> None:
    out = server.compare_document("VCU3-SRS", max_changes=2)
    assert len(out["changes"]) == 2
    assert out["truncated"] is True
    assert out["total_changes"] > 2


def test_compare_document_without_an_issue_needs_from(req_test_copy: Path) -> None:
    repo = pygit2.Repository(str(req_test_copy))
    repo.references.delete("refs/tags/VCU3-STS-issue-1")
    with pytest.raises(ValueError, match="no issue to compare from"):
        server.compare_document("VCU3-STS")
    out = server.compare_document("VCU3-STS", from_ref="HEAD~3")
    assert out["from"]["ref"] == "HEAD~3"


def test_compare_document_names_a_bad_version(req_test_copy: Path) -> None:
    with pytest.raises(ValueError, match="no-such-version"):
        server.compare_document("VCU3-SRS", from_ref="no-such-version")


def test_compare_document_works_read_only(
    req_test_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server._State, "read_only", True)
    assert server.compare_document("VCU3-SRS")["total_changes"] > 0


@pytest.mark.usefixtures("served")
@pytest.mark.parametrize("change, side", [("Added", "to"), ("Deleted", "from")])
def test_compare_document_reports_an_unreadable_links_file_of_a_whole_object(tmp_path, change, side):
    """MELLON-VER-SRS-27 (with -59): SPEC-9 exists at one version only, and its links
    file there isn't well-formed. The MCP result reports SPEC-9 as unreadable at that
    version, naming the links file, rather than as an object with no links."""
    r, _, c1 = build_spec(tmp_path)
    r.write("SPEC/objects/SPEC-9.xml", b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<object id="SPEC-9">\n  <description>New.</description>\n</object>\n')
    r.write("SPEC/links/SPEC-9.xml", b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<links source="SPEC-9">\n  <link target-doc="SPEC"\n')
    c2 = r.commit("SPEC-9 with a broken links file")
    assert mcp_server.main(["--repo-root", str(r.root)]) == 0, "setup: MCP server didn't start"
    from_ref, to_ref = (c1, c2) if change == "Added" else (c2, c1)
    out = mcp_server.compare_document("SPEC", from_ref=from_ref, to_ref=to_ref)
    (row,) = [c for c in out["changes"] if c["obj_id"] == "SPEC-9"]
    assert row["changes"] == [change], f"setup: SPEC-9 should be {change}: {row}"
    unreadable = row.get("unreadable") or {}
    other = "from" if side == "to" else "to"
    assert "SPEC/links/SPEC-9.xml" in (unreadable.get(side) or "") and unreadable.get(other) is None, (
        f"SPEC-9 should be unreadable at {side} only, naming its links file: {row}"
    )


# --- written blind from MELLON-VER-SRS (round 1) -----------------------------
#
# The compare tool, in process and over stdio. Written from MELLON-VER-SRS, the
# definitions and the public signatures alone, without reading the code under
# test or the existing tests. Every expected value is worked out by hand from a
# requirement or a definition; the docstring of each test names the
# requirement it checks. Each test builds its own git repository under tmp_path
# with fixed commit times, so a run repeats exactly.


# --------------------------------------------------------------------------
# The MCP server over stdio: MELLON-VER-SRS-26, -56, -59, -60
# --------------------------------------------------------------------------


def _mcp_command() -> str:
    exe = shutil.which("mellon-mcp")
    if exe is None:
        name = "mellon-mcp.exe" if os.name == "nt" else "mellon-mcp"
        candidate = Path(sys.executable).parent / name
        exe = str(candidate) if candidate.exists() else None
    if exe is None:
        pytest.fail("mellon-mcp isn't installed in this environment")
    return exe


def mcp_session(root: Path, calls: list[dict], log: Path, on_ready=None, on_done=None) -> list[tuple[bool, str]]:
    """Start the MCP server over stdio on `root`, call compare_document with each
    argument dict in `calls`, and return (is error, text) for each."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    params = StdioServerParameters(command=_mcp_command(), args=["--repo-root", str(root)], env=env)

    async def run() -> list[tuple[bool, str]]:
        results = []
        with open(log, "w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if on_ready:
                        on_ready()
                    for args in calls:
                        res = await session.call_tool("compare_document", args)
                        text = "".join(getattr(c, "text", "") or "" for c in res.content)
                        results.append((bool(res.isError), text))
                    if on_done:
                        on_done()
        return results

    return asyncio.run(asyncio.wait_for(run(), timeout=120))


def _is_error(result: tuple[bool, str]) -> bool:
    is_error, text = result
    if is_error:
        return True
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    return isinstance(payload, dict) and "error" in payload


def build_mcp_spec(tmp: Path) -> tuple[GitRepo, dict[str, str]]:
    """SPEC with Issue 1 on "seed" and the latest commit "edits":

    seed:  SPEC-0 Heading "Introduction"
             SPEC-1 "The VCU shall stop."   SPEC-2 "Requirement 2."   SPEC-3 "Requirement 3."
           SPEC-4 Heading "Interfaces"
             SPEC-5 "Requirement 5." -satisfies-> SPEC-1 (ack c0)
             SPEC-6 "Requirement 6." -refines-> SPEC-2 (ack c0)
             SPEC-7 "Requirement 7."
    edits: SPEC-1 -> "The VCU must stop."; SPEC-2 rationale (label "Rationale") set to
           "Because."; SPEC-3 deleted; SPEC-8 "Requirement 8." added under SPEC-0;
           SPEC-7 moved to the top level after SPEC-4; SPEC-5's link re-acknowledged
           (ack seed); SPEC-6's link to SPEC-2 removed and a link -satisfies-> SPEC-1 added.
    """
    r = GitRepo(tmp / "repo")
    f = editor.create_document(r.root, "SPEC", title="Spec")
    manifest = r.read("SPEC/SPEC.xml")
    plain = b'<attribute name="rationale" kind="text" required="false"/>'
    assert plain in manifest, "setup: rationale declaration not found"
    r.write("SPEC/SPEC.xml", manifest.replace(plain, plain[:-2] + b' label="Rationale"/>'))
    editor.add_attr_option(f, "type", "Requirement")
    texts = {"SPEC-1": "The VCU shall stop.", "SPEC-2": "Requirement 2.", "SPEC-3": "Requirement 3.",
             "SPEC-5": "Requirement 5.", "SPEC-6": "Requirement 6.", "SPEC-7": "Requirement 7."}
    ids = [editor.create_object(f, None, "Introduction")]
    ids += [editor.create_object(f, "SPEC-0", texts[i]) for i in ("SPEC-1", "SPEC-2", "SPEC-3")]
    ids.append(editor.create_object(f, None, "Interfaces"))
    ids += [editor.create_object(f, "SPEC-4", texts[i]) for i in ("SPEC-5", "SPEC-6", "SPEC-7")]
    assert ids == [f"SPEC-{i}" for i in range(8)], f"setup: unexpected ids {ids}"
    for obj_id in ids:
        editor.set_attribute(f, obj_id, "type", "Heading" if obj_id in ("SPEC-0", "SPEC-4") else "Requirement")
    c0 = r.commit("objects")
    r.write("SPEC/links/SPEC-5.xml", links_xml("SPEC-5", [("SPEC", "SPEC-1", "satisfies", c0[:7])]))
    r.write("SPEC/links/SPEC-6.xml", links_xml("SPEC-6", [("SPEC", "SPEC-2", "refines", c0[:7])]))
    seed = r.commit("seed")
    r.tag_issue("SPEC", "1", seed)

    editor.set_description(f, "SPEC-1", "The VCU must stop.")
    editor.set_attribute(f, "SPEC-2", "rationale", "Because.")
    assert editor.delete_object(f, "SPEC-3", force=True).performed, "setup: SPEC-3 not deleted"
    added = editor.create_object(f, "SPEC-0", "Requirement 8.")
    assert added == "SPEC-8", f"setup: added object got id {added}"
    editor.set_attribute(f, "SPEC-8", "type", "Requirement")
    editor.move_object(f, "SPEC-7", to_top=True, after="SPEC-4")
    r.write("SPEC/links/SPEC-5.xml", links_xml("SPEC-5", [("SPEC", "SPEC-1", "satisfies", seed[:7])]))
    r.write("SPEC/links/SPEC-6.xml", links_xml("SPEC-6", [("SPEC", "SPEC-1", "satisfies", seed[:7])]))
    latest = r.commit("edits")
    return r, {"c0": c0, "seed": seed, "latest": latest}


def _unique_prefix(repo: pygit2.Repository, hex_id: str, length: int) -> bool:
    prefix = hex_id[:length]
    return sum(1 for oid in repo.odb if str(oid).startswith(prefix)) == 1


@pytest.fixture(scope="module")
def mcp_spec(tmp_path_factory):
    """One MCP server session over stdio on the SPEC repository of `build_mcp_spec`.
    Returns (commits, results by call name)."""
    tmp = tmp_path_factory.mktemp("mcp_spec")
    r, commits = build_mcp_spec(tmp)
    seed = commits["seed"]
    assert _unique_prefix(r.repo, seed, 4), "setup: the 4-hex prefix of seed isn't unique"
    calls = {
        "issue-to-latest": {"doc_id": "SPEC", "from_ref": "SPEC-issue-1", "to_ref": diff.LATEST},
        "bad-to": {"doc_id": "SPEC", "from_ref": "SPEC-issue-1", "to_ref": "no-such-version"},
        "3-hex-from": {"doc_id": "SPEC", "from_ref": seed[:3], "to_ref": diff.LATEST},
        "4-hex-from": {"doc_id": "SPEC", "from_ref": seed[:4], "to_ref": diff.LATEST},
    }
    results = mcp_session(r.root, list(calls.values()), tmp / "mcp.log")
    return commits, dict(zip(calls, results))


def _payload(result: tuple[bool, str]) -> dict:
    is_error, text = result
    assert not is_error, f"the compare tool failed: {text[:500]}"
    return json.loads(text)


def _entry(payload: dict, obj_id: str) -> dict:
    found = [c for c in payload["changes"] if c["obj_id"] == obj_id]
    assert len(found) == 1, f"{obj_id} is listed {len(found)} times in the MCP result"
    return found[0]


def test_ver59_mcp_reports_each_changed_object_with_its_changes(mcp_spec):
    """MELLON-VER-SRS-59: Issue 1 -> latest commit lists exactly SPEC-1, -2, -6 Modified,
    SPEC-3 Deleted, SPEC-8 Added, SPEC-7 Moved and SPEC-5 Link reviewed."""
    _, results = mcp_spec
    payload = _payload(results["issue-to-latest"])
    got = {c["obj_id"]: sorted(c["changes"]) for c in payload["changes"]}
    expected = {"SPEC-1": ["Modified"], "SPEC-2": ["Modified"], "SPEC-3": ["Deleted"], "SPEC-5": ["Link reviewed"],
                "SPEC-6": ["Modified"], "SPEC-7": ["Moved"], "SPEC-8": ["Added"]}
    assert got == expected, f"MCP changes {got}"


def test_ver59_mcp_description_at_both(mcp_spec):
    """MELLON-VER-SRS-59: SPEC-1's description from 'The VCU shall stop.' to 'The VCU must stop.'."""
    _, results = mcp_spec
    entry = _entry(_payload(results["issue-to-latest"]), "SPEC-1")
    assert entry.get("description") == {"from": "The VCU shall stop.", "to": "The VCU must stop."}, (
        f"SPEC-1 description reported as {entry.get('description')}"
    )


def test_ver59_mcp_attribute_at_both_with_label(mcp_spec):
    """MELLON-VER-SRS-59 (with -45, -49): SPEC-2's rationale from not set to 'Because.',
    named by its label at To, 'Rationale'."""
    _, results = mcp_spec
    entry = _entry(_payload(results["issue-to-latest"]), "SPEC-2")
    attrs = [a for a in entry.get("attrs", []) if a.get("name") == "rationale"]
    got = [(a.get("label"), a.get("from"), a.get("to")) for a in attrs]
    assert got == [("Rationale", None, "Because.")], f"SPEC-2 rationale reported as {attrs}"


def test_ver59_mcp_deleted_object_fields_at_from(mcp_spec):
    """MELLON-VER-SRS-59 (with -50): SPEC-3, Deleted, comes with its fields at From:
    description 'Requirement 3.', type Requirement, under SPEC-0."""
    _, results = mcp_spec
    obj = _entry(_payload(results["issue-to-latest"]), "SPEC-3").get("object") or {}
    got = (obj.get("description"), (obj.get("attrs") or {}).get("type"), (obj.get("position") or {}).get("parent"))
    assert got == ("Requirement 3.", "Requirement", "SPEC-0"), f"SPEC-3 fields at From: {obj}"


def test_ver59_mcp_added_object_fields_at_to(mcp_spec):
    """MELLON-VER-SRS-59 (with -50): SPEC-8, Added, comes with its fields at To:
    description 'Requirement 8.', type Requirement, under SPEC-0."""
    _, results = mcp_spec
    obj = _entry(_payload(results["issue-to-latest"]), "SPEC-8").get("object") or {}
    got = (obj.get("description"), (obj.get("attrs") or {}).get("type"), (obj.get("position") or {}).get("parent"))
    assert got == ("Requirement 8.", "Requirement", "SPEC-0"), f"SPEC-8 fields at To: {obj}"


def test_ver59_mcp_moved_parent_and_position_at_both(mcp_spec):
    """MELLON-VER-SRS-59 (with -52): SPEC-7 under SPEC-4 after SPEC-6 at From, at the top
    level after SPEC-4 at To."""
    _, results = mcp_spec
    moved = _entry(_payload(results["issue-to-latest"]), "SPEC-7").get("moved") or {}
    got = {side: ((moved.get(side) or {}).get("parent"), (moved.get(side) or {}).get("after"))
           for side in ("from", "to")}
    assert got == {"from": ("SPEC-4", "SPEC-6"), "to": (None, "SPEC-4")}, f"SPEC-7 positions: {moved}"


def test_ver59_mcp_link_reviewed_ack_at_both(mcp_spec):
    """MELLON-VER-SRS-59 (with -53): SPEC-5's link to SPEC-1 with its acknowledged commit
    at From (c0) and at To (seed)."""
    commits, results = mcp_spec
    entry = _entry(_payload(results["issue-to-latest"]), "SPEC-5")
    records = [rec for group in (entry.get("links") or {}).values() for rec in group
               if isinstance(rec, dict) and rec.get("target_id") == "SPEC-1"]
    values = [set(map(str, rec.values())) for rec in records]
    ok = [v for v in values if {commits["c0"][:7], commits["seed"][:7]} <= v]
    assert len(ok) == 1, f"SPEC-5's reviewed link with acks {commits['c0'][:7]} -> {commits['seed'][:7]} not found in {entry}"


def test_ver59_mcp_link_added_and_removed(mcp_spec):
    """MELLON-VER-SRS-59 (with -46): SPEC-6's link -refines-> SPEC-2 removed and its link
    -satisfies-> SPEC-1 added."""
    _, results = mcp_spec
    links = _entry(_payload(results["issue-to-latest"]), "SPEC-6").get("links") or {}
    got = {kind: sorted((rec.get("target_id"), rec.get("type")) for rec in links.get(kind, []))
           for kind in ("added", "removed")}
    assert got == {"added": [("SPEC-1", "satisfies")], "removed": [("SPEC-2", "refines")]}, f"SPEC-6 links: {links}"


def test_ver60_mcp_unknown_to_version_error_names_it(mcp_spec):
    """MELLON-VER-SRS-60: to_ref 'no-such-version' gives an error naming 'no-such-version'."""
    _, results = mcp_spec
    result = results["bad-to"]
    assert _is_error(result) and "no-such-version" in result[1], f"expected an error naming it, got {result}"


def test_ver60_mcp_three_hex_prefix_error_names_it(mcp_spec):
    """MELLON-VER-SRS-60 (with -9: 4 hex digits is the shortest prefix git accepts): the
    first 3 hex digits of a real commit as from_ref resolve to no commit: an error naming
    the prefix."""
    commits, results = mcp_spec
    prefix = commits["seed"][:3]
    is_error, text = results["3-hex-from"]
    named = re.search(rf"(?<![0-9a-f]){prefix}(?![0-9a-f])", text) is not None
    assert _is_error(results["3-hex-from"]) and named, f"expected an error naming {prefix!r}, got {(is_error, text)}"


def test_ver60_mcp_four_hex_prefix_is_the_same_version(mcp_spec):
    """MELLON-VER-SRS-60 boundary (just on the limit): a unique 4-hex prefix of the Issue 1
    commit resolves to that commit, so the result is the same as from 'SPEC-issue-1'.
    Assumption: the MCP tool accepts the names SRS-9 lists for the dialog, since -59 says
    agents 'must get the same answer as a person'."""
    _, results = mcp_spec
    assert not _is_error(results["4-hex-from"]), f"a unique 4-hex prefix was refused: {results['4-hex-from']}"
    by_prefix = {c["obj_id"]: c["changes"] for c in _payload(results["4-hex-from"])["changes"]}
    by_issue = {c["obj_id"]: c["changes"] for c in _payload(results["issue-to-latest"])["changes"]}
    assert by_prefix == by_issue, f"from the prefix {by_prefix}, from the issue {by_issue}"


@pytest.fixture(scope="module")
def mcp_other_only(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("mcp_other")
    r, c1, c2 = build_other_only(tmp)
    repo = r.repo
    git_ops.create_tag(repo, "rel-a", pygit2.Oid(hex=c1), "release a")
    repo.branches.local.create("next", repo[pygit2.Oid(hex=c2)])
    calls = [{"doc_id": "SPEC", "from_ref": c1[:7], "to_ref": c2[:7]},
             {"doc_id": "SPEC", "from_ref": "rel-a", "to_ref": "next"}]
    return (c1, c2), mcp_session(r.root, calls, tmp / "mcp.log")


def test_ver26_mcp_error_names_document_and_both_hashes(mcp_other_only):
    """MELLON-VER-SRS-26 over MCP: SPEC between two 7-hex hashes in a repository holding
    only OTHER: an error naming SPEC and both hashes."""
    (c1, c2), results = mcp_other_only
    result = results[0]
    missing = [w for w in ("SPEC", c1[:7], c2[:7]) if w not in result[1]]
    assert _is_error(result) and not missing, f"expected an error naming SPEC and both, missing {missing}: {result}"


def test_ver26_mcp_error_names_document_and_both_names(mcp_other_only):
    """MELLON-VER-SRS-26 over MCP: SPEC between 'rel-a' and 'next' in a repository holding
    only OTHER: an error naming SPEC, 'rel-a' and 'next'."""
    _, results = mcp_other_only
    result = results[1]
    missing = [w for w in ("SPEC", "rel-a", "next") if w not in result[1]]
    assert _is_error(result) and not missing, f"expected an error naming SPEC and both, missing {missing}: {result}"


def test_ver56_mcp_compare_changes_no_file_index_or_reference(tmp_path):
    """MELLON-VER-SRS-56 over MCP: compare calls (latest -> working copy, Issue 1 -> latest,
    and a failing one) leave the working copy, the index, HEAD and every reference as
    they were once the server was up."""
    r, _ = build_snapshot_repo(tmp_path)
    snaps = []
    calls = [{"doc_id": "SPEC", "from_ref": diff.LATEST, "to_ref": diff.WORKING_COPY},
             {"doc_id": "SPEC", "from_ref": "SPEC-issue-1", "to_ref": diff.LATEST},
             {"doc_id": "SPEC", "from_ref": "no-such-version", "to_ref": diff.LATEST}]
    log = tmp_path / "mcp.log"
    results = mcp_session(r.root, calls, log,
                          on_ready=lambda: snaps.append(snapshot(r.root)),
                          on_done=lambda: snaps.append(snapshot(r.root)))
    assert [_is_error(x) for x in results] == [False, False, True], f"setup: unexpected results {results}"
    changed = snapshot_difference(snaps[0], snaps[1])
    assert not changed, f"comparing over MCP changed: {changed[:10]}"


# ==========================================================================
# MELLON-VER-SRS-9 / -10: typed versions
# ==========================================================================


@pytest.mark.usefixtures("served")
def test_srs10_ambiguous_prefix_refused_by_mcp(tmp_path):
    """MELLON-VER-SRS-10 (with -60): the MCP compare tool refuses an ambiguous prefix with an
    error naming it, and returns no comparison."""
    root = tmp_path / "r"
    _build(root)
    prefix = _ambiguous_prefix(root, "commit")
    assert mcp_server.main(["--repo-root", str(root)]) == 0, "setup: MCP server didn't start"
    try:
        out = mcp_server.compare_document("SPEC", from_ref=prefix, to_ref=LATEST)
    except Exception as exc:  # an error raised by the tool is how FastMCP reports one
        message, compared = str(exc), False
    else:
        message, compared = json.dumps(out, default=str), "changes" in out
    assert not compared, f"the MCP tool compared the ambiguous prefix {prefix}: {message[:300]}"
    assert prefix in message, f"the MCP error should name {prefix!r}: {message[:300]}"


# ==========================================================================
# MELLON-VER-SRS-46: links added and removed
# ==========================================================================


@pytest.mark.usefixtures("served")
def test_srs46_links_added_and_removed_through_mcp(tmp_path):
    """MELLON-VER-SRS-46 (with -59): the MCP compare tool gives the same added and removed links."""
    root = tmp_path / "r"
    before, after = _relinked_repo(root)
    assert mcp_server.main(["--repo-root", str(root)]) == 0, "setup: MCP server didn't start"
    out = mcp_server.compare_document("SPEC", from_ref=before, to_ref=after)
    (change,) = [c for c in out["changes"] if c["obj_id"] == "SPEC-1"]
    added = {(l["target_id"], l["type"]) for l in change["links"]["added"]}
    removed = {(l["target_id"], l["type"]) for l in change["links"]["removed"]}
    assert added == {("SPEC-2", "refines")}, f"added links: {change['links']}"
    assert removed == {("SPEC-4", "refines")}, f"removed links: {change['links']}"


# --- written blind from MELLON-VER-SRS (round 2) -----------------------------
#
# MELLON-VER-SRS-22, -56, -67 and -87 through the MCP server over stdio.
# Written from the requirements, docs/definitions.md and the public signatures
# alone, without the code under test or the bodies of other tests; every
# expected value comes from a requirement or a definition. Repositories are
# built in tmp_path with fixed commit signatures; no real clock, no network.


WORKING_COPY = diff.WORKING_COPY


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-22: differences gone in the canonical form are no change
# ---------------------------------------------------------------------------


def test_22_mcp_compare_reports_no_change_for_canonical_only_rewrite(canon_repo, tmp_path):
    """MELLON-VER-SRS-22 through the MCP server's compare tool, over stdio: the same
    rewrite as the dialog test; the tool returns no change."""
    root, info = canon_repo
    _rewrite(root, "**/*.xml", _everything)
    c2 = _r2_commit(root, "rewrite everything")
    [result] = mcp_session(root, [{"doc_id": DOC, "from_ref": info["c1"], "to_ref": c2}], tmp_path / "mcp.log")
    assert not _is_error(result), f"precondition: the compare tool failed: {result[1][:300]}"
    payload = _payload(result)
    assert payload["changes"] == [], f"a canonical-only rewrite is no change, the tool returns {payload['changes']}"


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-56: comparing changes nothing but by saving pending edits
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mcp_dirty(tmp_path_factory):
    """One MCP server session over stdio on the dirty repository, comparing two
    commits (named and typed), HEAD with the working copy and the working copy
    with a branch. Snapshots taken once the server is up and before it stops."""
    base = tmp_path_factory.mktemp("mcp56")
    root = base / "repo"
    info = _build_dirty_repo(root)
    snaps: dict[str, dict] = {}
    calls = [
        {"doc_id": DOC, "from_ref": info["c1"], "to_ref": info["c2"]},
        {"doc_id": DOC, "from_ref": "side", "to_ref": "HEAD"},
        {"doc_id": DOC, "from_ref": "HEAD~1", "to_ref": "v1"},
        {"doc_id": DOC, "from_ref": "HEAD", "to_ref": WORKING_COPY},
        {"doc_id": DOC, "from_ref": WORKING_COPY, "to_ref": "side"},
    ]
    results = mcp_session(
        root, calls, base / "mcp.log",
        on_ready=lambda: snaps.setdefault("before", _snapshot(root)),
        on_done=lambda: snaps.setdefault("after", _snapshot(root)),
    )
    return results, snaps


@pytest.mark.parametrize("part", PARTS)
def test_56_mcp_compare_changes_nothing(mcp_dirty, part):
    """MELLON-VER-SRS-56 through the MCP server's compare tool: five comparisons,
    two of them with the working copy, change nothing in `part`."""
    results, snaps = mcp_dirty
    failed = [r[1][:200] for r in results if _is_error(r)]
    assert not failed, f"precondition: comparisons failed: {failed}"
    changed = _changed(snaps["before"], snaps["after"], part)
    assert not changed, f"the MCP compare tool changed {sorted(changed)} in the {part}"


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-67: each result reports the commit each version resolved to,
# "by at least the first 7 digits of its hash" (settled with the person on
# 2026-11-07). The tests accept any run of 7 to 40 hex digits that begins the
# right commit's hash. They don't tie a hash to its side, but each side's
# commit differs, so the other side's hash can't stand in for it.
#
# MELLON-VER-SRS-87: the MCP server's compare tool returns the full hash.
# ---------------------------------------------------------------------------


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in _strings(k) + _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


MCP_67_CALLS = {
    "branch to HEAD~1": ({"from_ref": "feature", "to_ref": "HEAD~1"}, 2, 4),
    "defaults": ({}, 1, 5),
    "annotated tag to remote-tracking branch": ({"from_ref": "v1", "to_ref": "origin/review"}, 1, 3),
    "HEAD~1 to working copy": ({"from_ref": "HEAD~1", "to_ref": WORKING_COPY}, 4, None),
}


@pytest.fixture(scope="module")
def mcp_rev(rev_template, tmp_path_factory):
    """One MCP server session over stdio on a copy of the revisions repository."""
    src, info = rev_template
    base = tmp_path_factory.mktemp("mcp67")
    root = base / "repo"
    shutil.copytree(src, root)
    names = list(MCP_67_CALLS)
    calls = [{"doc_id": DOC, **MCP_67_CALLS[n][0]} for n in names]
    results = mcp_session(root, calls, base / "mcp.log")
    return info, dict(zip(names, results))


MCP_67_HASH_CASES = [
    (name, side) for name, (_, ka, kb) in MCP_67_CALLS.items() for side, k in (("From", ka), ("To", kb)) if k is not None
]


@pytest.mark.parametrize("name, side", MCP_67_HASH_CASES, ids=[f"{n}-{s}" for n, s in MCP_67_HASH_CASES])
def test_67_mcp_result_reports_hash_of_each_version(mcp_rev, name, side):
    """MELLON-VER-SRS-67 through the MCP server's compare tool: the result holds at
    least the first 7 digits of the hash of the commit each version resolved to,
    for typed names, an annotated tag (its commit, not the tag object) and the
    tool's defaults (latest issue, latest commit)."""
    info, results = mcp_rev
    result = results[name]
    assert not _is_error(result), f"precondition: the compare tool failed: {result[1][:300]}"
    _, ka, kb = MCP_67_CALLS[name]
    full = info["commits"][ka if side == "From" else kb]
    strings = _strings(_payload(result))
    assert any(_shows_hash_of(s, full) for s in strings), (
        f"{name}: {side} resolved to {full}; the result doesn't hold 7 or more of its first digits. "
        f"Top-level values: { {k: v for k, v in _payload(result).items() if k != 'changes'} }"
    )


@pytest.mark.parametrize("name, side", MCP_67_HASH_CASES, ids=[f"{n}-{s}" for n, s in MCP_67_HASH_CASES])
def test_87_mcp_result_reports_full_hash_of_each_version(mcp_rev, name, side):
    """MELLON-VER-SRS-87: the MCP server's compare tool returns the full 40-digit
    hash of the commit each version resolved to, for typed names, an annotated
    tag (its commit, not the tag object) and the tool's defaults (latest issue,
    latest commit). With To the working copy, only From has a commit."""
    info, results = mcp_rev
    result = results[name]
    assert not _is_error(result), f"precondition: the compare tool failed: {result[1][:300]}"
    _, ka, kb = MCP_67_CALLS[name]
    full = info["commits"][ka if side == "From" else kb]
    strings = _strings(_payload(result))
    assert any(full in s.lower() for s in strings), (
        f"{name}: {side} resolved to {full}; the result doesn't hold that full hash. "
        f"Top-level values: { {k: v for k, v in _payload(result).items() if k != 'changes'} }"
    )


def test_67_mcp_result_says_to_was_the_working_copy(mcp_rev):
    """MELLON-VER-SRS-67 through the MCP server: with To the working copy, the
    result says so. Weak: echoing the version's name is enough to pass."""
    info, results = mcp_rev
    result = results["HEAD~1 to working copy"]
    assert not _is_error(result), f"precondition: the compare tool failed: {result[1][:300]}"
    strings = _strings(_payload(result))
    assert any("working copy" in s.lower() for s in strings), "the result doesn't say To was the working copy"

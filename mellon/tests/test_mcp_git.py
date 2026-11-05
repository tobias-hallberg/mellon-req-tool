"""Tests for the MCP git/review tools (Slice M3).

`req_test_copy` in conftest could be reused, but it currently lives in
test_mcp_write.py. Tests here re-declare the fixture so the two files stay
independent — copies of req-test/ are cheap.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pygit2
import pytest
from lxml import etree

from mellon.mcp import server


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

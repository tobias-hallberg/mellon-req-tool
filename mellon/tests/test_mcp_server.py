"""Tests of the MCP server as a whole, from MELLON-MCP-SRS.

Starting, the folder to serve and exit statuses, the version, logging, standard
output and standard error, the network, the offered tools and parameters, the
read-only server, other processes and overlapping calls, and paths outside the
repository. Written blind, from the requirements, the definitions and the public
signatures. Each test starts the installed ``mellon-mcp`` program and checks what an
agent's client receives over standard input and output, standard error and the exit
status.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import re
import shutil
import string
import subprocess
import sys
import uuid
from pathlib import Path

import mcp.types
import pytest
from lxml import etree

from tests.mcp_stdio_support import (
    FIXTURE_DOCS,
    MINI_MANIFEST,
    MINI_OBJECT,
    PROJECT_ROOT,
    SVG,
    TABLE,
    WINDOWS,
    RawServer,
    append_to_description,
    changed,
    child_env,
    clone_of,
    doc_ids,
    failed,
    git,
    make_doc_repo,
    make_empty_repo,
    mcp_server,
    network_sockets,
    norm_text,
    payload,
    process_tree,
    ref_state,
    requires_mellon_mcp,
    result_text,
    reversed_outline,
    run_mcp,
    show,
    standin_programs,
    succeeded,
    tree_hashes,
    without_roots,
    write_lf,
)
from tests.mcp_stdio_support import (  # noqa: F401 - module fixtures
    ident_home,
    neutral_dir,
    plain_template,
    rich_template,
)

pytestmark = requires_mellon_mcp


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


SPEC_OBJECTS = PROJECT_ROOT / "MELLON-MCP-SRS" / "objects"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
NO_VALUE = "—"  # the table's "—": no default
NO_DEFAULT = "<none>"
SERVING = "mellon-mcp serving"  # the line MELLON-MCP-SRS-15's rationale names


# list_tools of the previous release (0.3.0, the Windows installer build),
# recorded once and normalised as describe_schema() does:
# tool -> parameter -> [type, required, default ("<none>" when it has none)].
RELEASE_0_3_0_TOOLS = r"""{
    "list_documents": {"include": ["array of strings or null", false, null]},
    "show_document": {"doc_id": ["string", true, "<none>"], "include": ["array of strings or null", false, null]},
    "search_objects": {"query": ["string", true, "<none>"], "doc_ids": ["array of strings or null", false, null], "attr_filters": ["object of strings or null", false, null], "max_results": ["integer", false, 50]},
    "show_object": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"]},
    "inspect_object": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "include_trace": ["boolean", false, true], "trace_depth": ["integer", false, 2], "include_history": ["boolean", false, true], "history_max": ["integer", false, 5]},
    "outline": {"doc_id": ["string", true, "<none>"]},
    "validate_document": {"doc_id": ["string", true, "<none>"]},
    "validate_repo": {},
    "repo_health": {},
    "suspects": {"doc_id": ["string or null", false, null]},
    "trace": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "direction": ["one of up, down, both", false, "both"], "max_depth": ["integer or null", false, 2]},
    "incoming_links": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"]},
    "coverage_report": {"source_doc": ["string", true, "<none>"], "target_doc": ["string", true, "<none>"], "direction": ["one of incoming, outgoing", false, "incoming"], "link_type": ["string or null", false, null], "include_headings": ["boolean", false, false], "transitive": ["boolean", false, false], "include_lists": ["boolean", false, false]},
    "new_object": {"doc_id": ["string", true, "<none>"], "parent_id": ["string or null", false, null], "description": ["string", false, ""], "attrs": ["object of strings or null", false, null], "links": ["array of objects or null", false, null], "commit_message": ["string or null", false, null]},
    "set_attribute": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "name": ["string", true, "<none>"], "value": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "edit_description": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "text": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "update_object": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "description": ["string or null", false, null], "attrs": ["object of strings or null", false, null], "add_links": ["array of objects or null", false, null], "remove_links": ["array of objects or null", false, null], "commit_message": ["string or null", false, null]},
    "copy_object": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "parent_id": ["string or null", false, null], "count": ["integer", false, 1], "copy_links": ["boolean", false, true], "copy_figure": ["boolean", false, true], "commit_message": ["string or null", false, null]},
    "bulk_set_attribute": {"doc_id": ["string", true, "<none>"], "obj_ids": ["array of strings", true, "<none>"], "name": ["string", true, "<none>"], "value": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "set_figure": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "source_path": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "clear_figure": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "set_table": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "table": ["object", true, "<none>"], "commit_message": ["string or null", false, null]},
    "clear_table": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "edit_metadata": {"doc_id": ["string", true, "<none>"], "field": ["string", true, "<none>"], "value": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "tag_issue": {"doc_id": ["string", true, "<none>"], "issue": ["string", true, "<none>"]},
    "add_parent_doc": {"doc_id": ["string", true, "<none>"], "parent_doc": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "remove_parent_doc": {"doc_id": ["string", true, "<none>"], "parent_doc": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "add_attr_option": {"doc_id": ["string", true, "<none>"], "attr_name": ["string", true, "<none>"], "option": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "define_attribute": {"doc_id": ["string", true, "<none>"], "name": ["string", true, "<none>"], "kind": ["string", true, "<none>"], "required": ["boolean", false, false], "options": ["array of strings or null", false, null], "label": ["string", false, ""], "default_value": ["string or null", false, null], "commit_message": ["string or null", false, null]},
    "change_attribute_required": {"doc_id": ["string", true, "<none>"], "name": ["string", true, "<none>"], "required": ["boolean", true, "<none>"], "commit_message": ["string or null", false, null]},
    "delete_attribute": {"doc_id": ["string", true, "<none>"], "name": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "remove_attr_option": {"doc_id": ["string", true, "<none>"], "attr_name": ["string", true, "<none>"], "option": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "manage_view": {"doc_id": ["string", true, "<none>"], "action": ["one of create, rename, delete, edit", true, "<none>"], "slug": ["string or null", false, null], "name": ["string or null", false, null], "visible_attrs": ["array of strings or null", false, null], "commit_message": ["string or null", false, null]},
    "create_document": {"doc_id": ["string", true, "<none>"], "title": ["string", false, ""], "parent_docs": ["array of strings or null", false, null], "attr_schema": ["array of objects or null", false, null], "commit_message": ["string or null", false, null]},
    "move_object": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "parent_id": ["string or null", false, null], "to_top": ["boolean", false, false], "before": ["string or null", false, null], "after": ["string or null", false, null], "commit_message": ["string or null", false, null]},
    "reorder_outline": {"doc_id": ["string", true, "<none>"], "ordering": ["array of objects", true, "<none>"], "commit_message": ["string or null", false, null]},
    "add_link": {"source_doc": ["string", true, "<none>"], "source_id": ["string", true, "<none>"], "target_doc": ["string", true, "<none>"], "target_id": ["string", true, "<none>"], "link_type": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "remove_link": {"source_doc": ["string", true, "<none>"], "source_id": ["string", true, "<none>"], "target_doc": ["string", true, "<none>"], "target_id": ["string", true, "<none>"], "commit_message": ["string or null", false, null]},
    "delete_object": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "force": ["boolean", false, false], "commit_message": ["string or null", false, null]},
    "ack_suspects": {"doc_id": ["string", true, "<none>"], "dry_run": ["boolean", false, false], "link_keys": ["array of objects or null", false, null], "commit_message": ["string or null", false, null]},
    "commit_changes": {"message": ["string", true, "<none>"], "paths": ["array of strings or null", false, null], "include_trailer": ["boolean", false, true]},
    "git_history": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "max_entries": ["integer", false, 10]},
    "show_object_at": {"doc_id": ["string", true, "<none>"], "obj_id": ["string", true, "<none>"], "commit_ref": ["string", true, "<none>"]},
    "review_suspect_link": {"source_doc": ["string", true, "<none>"], "source_id": ["string", true, "<none>"], "target_doc": ["string", true, "<none>"], "target_id": ["string", true, "<none>"]},
    "export_document": {"doc_id": ["string", true, "<none>"], "out_path": ["string", true, "<none>"], "attrs": ["array of strings or null", false, null], "rev": ["string or null", false, null]}
}"""


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tools_session(tmp_path_factory, plain_template, ident_home, neutral_dir):
    """initialize and list_tools of one server on a req-test copy."""
    base = tmp_path_factory.mktemp("tools")
    root = clone_of(plain_template, base / "repo")
    with mcp_server(
        ["--repo-root", root], env=child_env(ident_home), cwd=neutral_dir, stderr_path=base / "stderr.txt"
    ) as srv:
        return {"init": srv.init, "tools": {t.name: t for t in srv.tools()}}


# ---------------------------------------------------------------------------
# Choosing the repository: MELLON-MCP-SRS-2, -3, -5, -6
# ---------------------------------------------------------------------------


def _three_repos(base: Path, env) -> tuple[Path, Path, Path]:
    return (
        make_doc_repo(base / "a", "ALPHA", env),
        make_doc_repo(base / "b", "BRAVO", env),
        make_doc_repo(base / "c", "CHARLIE", env),
    )


def test_mcp_srs_2_answers_only_from_the_folder_chosen_at_start(tmp_path, ident_home):
    """MELLON-MCP-SRS-2: the MCP server shall answer each tool call from the folder to
    serve chosen when it started.

    Started with --repo-root A, the working directory B and CLAUDE_PROJECT_DIR C, the
    answers describe only A, also after set_figure reads a file in B and
    export_document writes into B; B and C keep their files.
    """
    env0 = child_env(ident_home)
    a, b, c = _three_repos(tmp_path, env0)
    picture = b / "picture.svg"
    write_lf(picture, SVG)
    out_pdf = b / "out.pdf"
    env = child_env(
        ident_home, CLAUDE_PROJECT_DIR=str(c), **standin_programs(tmp_path / "standins", tmp_path / "logs")
    )
    b_before = tree_hashes(b, include_git=True)
    c_before = tree_hashes(c, include_git=True)

    with mcp_server(["--repo-root", a], env=env, cwd=b, stderr_path=tmp_path / "stderr.txt") as srv:
        first = srv.call("list_documents")
        figure = srv.call("set_figure", {"doc_id": "ALPHA", "obj_id": "ALPHA-1", "source_path": str(picture)})
        export = srv.call("export_document", {"doc_id": "ALPHA", "out_path": str(out_pdf)})
        second = srv.call("list_documents")
        shown = srv.call("show_document", {"doc_id": "ALPHA"})
        other = srv.call("show_document", {"doc_id": "BRAVO"})

    assert doc_ids(first) == ["ALPHA"], f"first list_documents: {show(first)}"
    assert payload(second) == payload(first), "list_documents changed after calls naming files in B"
    assert succeeded(figure), f"set_figure: {show(figure)}"
    assert (a / "ALPHA" / "figures" / "ALPHA-1.svg").is_file(), "set_figure didn't write the figure into A"
    assert not shown.isError and payload(shown).get("doc_id") == "ALPHA", f"show_document ALPHA: {show(shown)}"
    assert failed(other), f"show_document BRAVO answered from B: {show(other)}"
    for res in (first, figure, export, second, shown):
        assert "BRAVO" not in result_text(res) and "CHARLIE" not in result_text(res), show(res)
    b_after = tree_hashes(b, include_git=True)
    b_after.pop("out.pdf", None)
    assert b_after == b_before, f"files changed in B: {sorted(changed(b_before, b_after))}"
    assert tree_hashes(c, include_git=True) == c_before, "files changed in C"


def test_mcp_srs_3_repo_root_names_the_folder_to_serve(tmp_path, plain_template, ident_home, neutral_dir):
    """MELLON-MCP-SRS-3: started with --repo-root <path>, the MCP server shall take the
    folder <path> names as the folder to serve.

    The path holds a space and non-ASCII letters; initialize succeeds and list_documents
    lists the fixture's three documents.
    """
    root = clone_of(plain_template, tmp_path / "Mellon prov å中" / "repo")
    with mcp_server(
        ["--repo-root", root], env=child_env(ident_home), cwd=neutral_dir, stderr_path=tmp_path / "stderr.txt"
    ) as srv:
        res = srv.call("list_documents")
    assert set(doc_ids(res)) == FIXTURE_DOCS, show(res)


def test_mcp_srs_5_claude_project_dir_names_the_folder_to_serve(tmp_path, ident_home):
    """MELLON-MCP-SRS-5: started without --repo-root while CLAUDE_PROJECT_DIR is set and
    not empty, the MCP server shall serve the folder CLAUDE_PROJECT_DIR names.

    CLAUDE_PROJECT_DIR names A and the working directory is B: list_documents lists A's
    document only.
    """
    a, b, _ = _three_repos(tmp_path, child_env(ident_home))
    env = child_env(ident_home, CLAUDE_PROJECT_DIR=str(a))
    with mcp_server([], env=env, cwd=b, stderr_path=tmp_path / "stderr.txt") as srv:
        res = srv.call("list_documents")
    assert doc_ids(res) == ["ALPHA"], show(res)


def test_mcp_srs_6_working_directory_is_the_folder_to_serve(tmp_path, ident_home):
    """MELLON-MCP-SRS-6: started without --repo-root while CLAUDE_PROJECT_DIR is unset,
    the MCP server shall serve the current working directory.

    No CLAUDE_PROJECT_DIR in the environment, the working directory B: list_documents
    lists B's document only.
    """
    _, b, _ = _three_repos(tmp_path, child_env(ident_home))
    env = child_env(ident_home)
    assert "CLAUDE_PROJECT_DIR" not in env
    with mcp_server([], env=env, cwd=b, stderr_path=tmp_path / "stderr.txt") as srv:
        res = srv.call("list_documents")
    assert doc_ids(res) == ["BRAVO"], show(res)


# ---------------------------------------------------------------------------
# Refused folders: MELLON-MCP-SRS-8, -9, -10
# ---------------------------------------------------------------------------


def _current_sid() -> str:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout
    return out.strip().split(",")[-1].strip('"')


@contextlib.contextmanager
def unlistable(folder: Path):
    """Make ``folder`` impossible to list for this user while the block runs."""
    if WINDOWS:
        sid = _current_sid()
        subprocess.run(["icacls", str(folder), "/deny", f"*{sid}:(RD)"], check=True, capture_output=True)
        try:
            yield
        finally:
            subprocess.run(["icacls", str(folder), "/remove:d", f"*{sid}"], check=True, capture_output=True)
    else:
        if os.geteuid() == 0:
            pytest.skip("root can list any folder, so the case can't be built")
        mode = folder.stat().st_mode
        folder.chmod(0o311)
        try:
            yield
        finally:
            folder.chmod(mode)


@contextlib.contextmanager
def refused_folder(case: str, base: Path, template: Path, env):
    """Yield (cwd, relative path, absolute path) of a folder the server must refuse."""
    if case == "outside_repository":
        folder = base / "plain-folder"
        write_lf(folder / "note.txt", "not a repository\n")
        yield base, folder.name, folder
    elif case == "document_folder":
        clone = clone_of(template, base / "clone")
        yield clone, "VCU3-SRS", clone / "VCU3-SRS"
    elif case == "file":
        clone = clone_of(template, base / "clone")
        yield clone / "VCU3-SRS", "VCU3-SRS.xml", clone / "VCU3-SRS" / "VCU3-SRS.xml"
    elif case == "missing_path":
        yield base, "no-such-folder", base / "no-such-folder"
    elif case == "shallow_clone":
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", template.as_uri(), str(base / "shallow")],
            check=True,
            capture_output=True,
            env=env,
        )
        assert (base / "shallow" / ".git" / "shallow").is_file(), "setup: the clone isn't shallow"
        yield base, "shallow", base / "shallow"
    elif case == "broken_git_folder":
        clone = clone_of(template, base / "clone")
        write_lf(clone / ".git" / "HEAD", "this is not a ref\n")
        r = subprocess.run(["git", "-C", str(clone), "rev-parse", "--git-dir"], capture_output=True, env=env)
        assert r.returncode != 0, "setup: git still reads the broken folder as a repository"
        yield base, "clone", clone
    elif case == "unlistable_root":
        clone = clone_of(template, base / "clone")
        with unlistable(clone):
            try:
                os.listdir(clone)
            except PermissionError:
                pass
            else:
                pytest.skip("this platform still lists the folder, so the case can't be built")
            yield base, "clone", clone
    else:  # pragma: no cover
        raise ValueError(case)


@pytest.mark.parametrize(
    "case",
    [
        "outside_repository",
        "document_folder",
        "file",
        "missing_path",
        "shallow_clone",
        "broken_git_folder",
    ],
)
def test_mcp_srs_8_exit_2_when_folder_is_not_a_repository_root(case, tmp_path, plain_template, ident_home):
    """MELLON-MCP-SRS-8: if the folder to serve isn't the root folder of a repository,
    the MCP server shall exit with status 2 without answering an MCP message.

    For each kind of folder, an initialize request on standard input gets no answer on
    standard output, and the status is 2. A clone whose .git/HEAD git can't read is no
    repository either.
    """
    env = child_env(ident_home)
    with refused_folder(case, tmp_path, plain_template, env) as (cwd, _rel, folder):
        r = run_mcp(["--repo-root", folder], env=env, cwd=cwd)
    assert r.returncode == 2, f"status {r.returncode}, stderr {r.stderr[-400:]!r}"
    assert r.stdout == b"", f"answered on standard output: {r.stdout[:400]!r}"


def test_mcp_srs_8_serves_a_root_whose_document_fails_to_load(tmp_path, plain_template, ident_home, neutral_dir):
    """MELLON-MCP-SRS-8: only a folder that isn't a repository's root folder ends the start
    with status 2.

    The other side of the condition: a clone whose VCU3-STS has a load error (conflict
    markers in an object file) is a repository's root, so the server answers initialize
    and list_documents still lists the three documents.
    """
    root = clone_of(plain_template, tmp_path / "repo")
    write_lf(
        root / "VCU3-STS" / "objects" / "VCU3-STS-1.xml",
        "<<<<<<< HEAD\n<object id=\"VCU3-STS-1\"/>\n=======\n<object id=\"VCU3-STS-1\">\n>>>>>>> other\n",
    )
    with mcp_server(
        ["--repo-root", root], env=child_env(ident_home), cwd=neutral_dir, stderr_path=tmp_path / "stderr.txt"
    ) as srv:
        res = srv.call("list_documents")
    assert set(doc_ids(res)) == FIXTURE_DOCS, show(res)


def test_mcp_srs_9_exit_2_when_folder_cannot_be_listed(tmp_path, plain_template, ident_home):
    """MELLON-MCP-SRS-9: if the MCP server can't list the folders in the folder to serve,
    it shall exit with status 2 without answering an MCP message.

    The clone's root folder denies listing; an initialize request gets no answer, and
    the status is 2.
    """
    env = child_env(ident_home)
    with refused_folder("unlistable_root", tmp_path, plain_template, env) as (cwd, _rel, folder):
        r = run_mcp(["--repo-root", folder], env=env, cwd=cwd)
    assert r.returncode == 2, f"status {r.returncode}, stderr {r.stderr[-400:]!r}"
    assert r.stdout == b"", f"answered on standard output: {r.stdout[:400]!r}"


@pytest.mark.parametrize("level", ["default", "ERROR"])
@pytest.mark.parametrize("case", ["outside_repository", "document_folder", "missing_path", "unlistable_root"])
def test_mcp_srs_10_exit_2_writes_reason_and_absolute_path(case, level, tmp_path, plain_template, ident_home):
    """MELLON-MCP-SRS-10: when the MCP server exits with status 2, it shall write the
    reason and the absolute path of the refused folder to standard error, whatever the
    log level.

    --repo-root is given relative to the working directory, without --log-level and with
    --log-level ERROR.
    """
    env = child_env(ident_home)
    with refused_folder(case, tmp_path, plain_template, env) as (cwd, rel, folder):
        args = ["--repo-root", rel] + (["--log-level", level] if level != "default" else [])
        r = run_mcp(args, env=env, cwd=cwd)
    stderr = r.stderr.decode("utf-8", errors="replace")
    assert r.returncode == 2, f"precondition: status {r.returncode}, not 2"
    absolute = str(folder.resolve() if folder.exists() else folder)
    assert norm_text(absolute) in norm_text(stderr), f"no absolute path {absolute!r} in {stderr[-600:]!r}"
    rest = norm_text(stderr).replace(norm_text(absolute), " ")
    assert re.search(r"[A-Za-z]{3,}", rest), f"no reason beside the path in {stderr[-600:]!r}"


# ---------------------------------------------------------------------------
# A repository without documents, log levels: MELLON-MCP-SRS-11, -12, -15, -120, -121
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("history", ["no_commit", "one_commit"])
def test_mcp_srs_11_serves_a_repository_without_documents(history, tmp_path, ident_home, neutral_dir):
    """MELLON-MCP-SRS-11: if the folder to serve holds no document, the MCP server shall
    start serving the folder.

    A git repository with no commit, and one with a commit of a README only: initialize
    succeeds, list_documents returns [] and validate_repo answers ok true.
    """
    env = child_env(ident_home)
    root = make_empty_repo(tmp_path / "empty", env, commit=history == "one_commit")
    with mcp_server(["--repo-root", root], env=env, cwd=neutral_dir, stderr_path=tmp_path / "stderr.txt") as srv:
        listed = srv.call("list_documents")
        validated = srv.call("validate_repo")
    assert not listed.isError and payload(listed) == [], show(listed)
    assert not validated.isError and payload(validated).get("ok") is True, show(validated)


NO_DOC_WARNING = re.compile(r"(?i)\b(no|zero|0)\b[^\n]*\bdocuments?\b")


def _no_doc_warnings(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if NO_DOC_WARNING.search(line) and SERVING not in line]


def _lines(stderr: str) -> set[str]:
    return {line.rstrip() for line in stderr.splitlines() if line.strip()}


@pytest.fixture(scope="module")
def level_runs(tmp_path_factory, plain_template, ident_home):
    """Standard error of a clone and of a repository without documents, per log level.

    --repo-root is relative to the working directory. The default-level and INFO runs
    of the clone come last and one after the other, so they meet the same state.
    """
    base = tmp_path_factory.mktemp("levels")
    env = child_env(ident_home)
    clone = clone_of(plain_template, base / "clone")
    empty = make_empty_repo(base / "empty", env, commit=True)
    order = [("clone", "DEBUG"), ("clone", "WARNING"), ("clone", "ERROR"), ("clone", None), ("clone", "INFO")]
    order += [("empty", level) for level in LEVELS]
    stderr = {}
    for repo, level in order:
        err = base / f"stderr-{repo}-{level or 'default'}.txt"
        args = ["--repo-root", repo] + (["--log-level", level] if level else [])
        with mcp_server(args, env=env, cwd=base, stderr_path=err) as srv:
            srv.call("list_documents")
            if repo == "clone":
                srv.call("show_object", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1"})
        stderr[(repo, level)] = err.read_text(encoding="utf-8", errors="replace")
    return {"clone": clone, "empty": empty, "stderr": stderr}


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING"])
def test_mcp_srs_12_warns_when_repository_has_no_document(level, level_runs):
    """MELLON-MCP-SRS-12: if the folder to serve holds no document while the log level is
    DEBUG, INFO or WARNING, the MCP server shall write a warning to standard error."""
    stderr = level_runs["stderr"][("empty", level)]
    assert _no_doc_warnings(stderr), f"no warning about a repository without documents in {stderr[-800:]!r}"


@pytest.mark.parametrize("level", ["DEBUG", "INFO"])
def test_mcp_srs_15_serving_line_gives_absolute_root(level, level_runs):
    """MELLON-MCP-SRS-15: when the MCP server starts serving while the log level is DEBUG
    or INFO, it shall write the absolute path of the repository's root folder to
    standard error.

    --repo-root was given as a relative path, so finding the whole absolute path shows
    that the written path is absolute.
    """
    stderr = level_runs["stderr"][("clone", level)]
    absolute = str(level_runs["clone"].resolve())
    assert norm_text(absolute) in norm_text(stderr), f"no {absolute!r} in {stderr[-800:]!r}"


@pytest.mark.parametrize("repo", ["clone", "empty"])
def test_mcp_srs_120_each_level_writes_a_subset_of_the_level_below(repo, level_runs):
    """MELLON-MCP-SRS-120: with --log-level DEBUG, INFO, WARNING or ERROR, the MCP server
    shall write only the log messages at that level or a more severe one.

    The same calls under each level: what ERROR writes WARNING writes too, what WARNING
    writes INFO writes too, and what INFO writes DEBUG writes too.
    """
    runs = level_runs["stderr"]
    for lower, higher in (("DEBUG", "INFO"), ("INFO", "WARNING"), ("WARNING", "ERROR")):
        extra = _lines(runs[(repo, higher)]) - _lines(runs[(repo, lower)])
        assert not extra, f"{higher} wrote lines that {lower} didn't: {sorted(extra)[:5]}"


@pytest.mark.parametrize("repo", ["clone", "empty"])
def test_mcp_srs_120_serving_line_only_under_debug_and_info(repo, level_runs):
    """MELLON-MCP-SRS-120: under WARNING or ERROR, the MCP server shall leave out INFO
    messages.

    The serving line is an INFO message (MELLON-MCP-SRS-15, -121): it appears under
    DEBUG and INFO and not under WARNING or ERROR.
    """
    runs = level_runs["stderr"]
    for level in ("DEBUG", "INFO"):
        assert SERVING in runs[(repo, level)], f"precondition: no serving line under {level}"
    for level in ("WARNING", "ERROR"):
        lines = [line for line in runs[(repo, level)].splitlines() if SERVING in line]
        assert not lines, f"{level} wrote the INFO serving line: {lines}"


def test_mcp_srs_120_no_document_warning_hidden_under_error(level_runs):
    """MELLON-MCP-SRS-120: under ERROR, the MCP server shall leave out WARNING messages.

    The warning that the repository holds no document (MELLON-MCP-SRS-12) appears under
    WARNING and not under ERROR.
    """
    runs = level_runs["stderr"]
    assert _no_doc_warnings(runs[("empty", "WARNING")]), "precondition: no warning under WARNING"
    lines = _no_doc_warnings(runs[("empty", "ERROR")])
    assert not lines, f"ERROR wrote the WARNING message: {lines}"


def test_mcp_srs_121_default_log_level_is_info(level_runs):
    """MELLON-MCP-SRS-121: started without --log-level, the MCP server shall use the log
    level INFO.

    The same calls without --log-level and with --log-level INFO give the same standard
    error, which holds the serving line.
    """
    runs = level_runs["stderr"]
    default, info = runs[("clone", None)], runs[("clone", "INFO")]
    assert SERVING in default, f"no serving line without --log-level: {default[-600:]!r}"
    assert default.splitlines() == info.splitlines(), (
        f"without --log-level: {default.splitlines()}\nwith INFO: {info.splitlines()}"
    )


# ---------------------------------------------------------------------------
# Version: MELLON-MCP-SRS-20, -21
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["outside_repository", "repo_root_not_a_repository", "project_dir_missing"])
def test_mcp_srs_20_version_option_writes_version_and_exits_0(where, tmp_path, ident_home):
    """MELLON-MCP-SRS-20: started with --version, the MCP server shall write
    'mellon-mcp <version>' to standard output and exit with status 0, whatever the
    folder to serve."""
    version = importlib.metadata.version("mellon")
    args: list = ["--version"]
    extra = {}
    if where == "repo_root_not_a_repository":
        plain = tmp_path / "plain"
        plain.mkdir()
        args = ["--repo-root", plain, "--version"]
    elif where == "project_dir_missing":
        extra["CLAUDE_PROJECT_DIR"] = str(tmp_path / "missing")
    r = run_mcp(args, env=child_env(ident_home, **extra), cwd=tmp_path, stdin=b"")
    assert r.returncode == 0, f"status {r.returncode}, stderr {r.stderr[-400:]!r}"
    assert r.stdout.decode("utf-8") in (f"mellon-mcp {version}\n", f"mellon-mcp {version}\r\n"), r.stdout


def test_mcp_srs_21_reports_the_version_option_version_on_connect(tools_session, tmp_path, ident_home):
    """MELLON-MCP-SRS-21: when an agent connects, the MCP server shall report as the
    server version the same <version> that --version writes."""
    written = run_mcp(["--version"], env=child_env(ident_home), cwd=tmp_path, stdin=b"").stdout.decode().strip()
    assert written.startswith("mellon-mcp "), f"precondition: --version wrote {written!r}"
    assert tools_session["init"].serverInfo.version == written[len("mellon-mcp ") :]


# ---------------------------------------------------------------------------
# Standard output, standard error, network: MELLON-MCP-SRS-25, -117, -118
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def raw_run(tmp_path_factory, rich_template, ident_home, neutral_dir):
    """One session on raw pipes: a read, compare_document, a write and an export of a
    document with a figure, with stand-ins for the typesetter and draw.io that print.
    The server's sockets are listed while the session is still open."""
    base = tmp_path_factory.mktemp("raw")
    root = clone_of(rich_template, base / "repo")
    log_dir = base / "standin-logs"
    env = child_env(ident_home, **standin_programs(base / "standins", log_dir))
    srv = RawServer(["--repo-root", root], env=env, cwd=neutral_dir, stderr_path=base / "stderr.txt")
    answers = {}
    try:
        answers["initialize"] = srv.initialize()
        answers["tools/list"] = srv.request("tools/list", {})
        answers["list_documents"] = srv.call_tool("list_documents", {})
        answers["compare_document"] = srv.call_tool("compare_document", {"doc_id": "VCU3-SRS"})
        answers["set_attribute"] = srv.call_tool(
            "set_attribute", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-2", "name": "status", "value": "Reviewed"}
        )
        answers["export_document"] = srv.call_tool(
            "export_document", {"doc_id": "VCU3-SRS", "out_path": str(base / "export.pdf")}
        )
        if WINDOWS or sys.platform.startswith("linux"):
            sockets = network_sockets(process_tree(srv.proc.pid))
        else:
            sockets = None
    finally:
        srv.close()
    return {
        "answers": answers,
        "stdout": srv.stdout_lines,
        "stderr": (base / "stderr.txt").read_text(encoding="utf-8", errors="replace"),
        "sockets": sockets,
        "standin_logs": {p.stem: p.read_text(encoding="utf-8") for p in log_dir.glob("*.log")},
    }


def test_mcp_srs_25_does_not_listen_for_network_connections(raw_run):
    """MELLON-MCP-SRS-25: the MCP server shall not listen for network connections.

    After initialize, list_documents, compare_document, a write and an export, the
    server's processes hold no listening TCP socket and no bound UDP socket.
    """
    if raw_run["sockets"] is None:
        pytest.skip("listing a process's sockets is built for Windows and Linux only")
    assert raw_run["sockets"] == [], f"network sockets: {raw_run['sockets']}"


def test_mcp_srs_117_standard_output_holds_only_protocol_messages(raw_run):
    """MELLON-MCP-SRS-117: while serving, the MCP server shall write nothing to standard
    output other than Model Context Protocol messages.

    Each line on standard output parses as a JSON-RPC 2.0 message, also while the export
    runs a stand-in typesetter and draw.io that print to their standard output.
    """
    assert {"tectonic", "drawio"} <= set(raw_run["standin_logs"]), (
        f"precondition: the stand-ins didn't both run ({sorted(raw_run['standin_logs'])}); "
        f"export answered {raw_run['answers']['export_document']}"
    )
    bad = []
    for line in raw_run["stdout"]:
        try:
            message = json.loads(line.decode("utf-8"))
        except ValueError:
            bad.append(line)
            continue
        if not (
            isinstance(message, dict)
            and message.get("jsonrpc") == "2.0"
            and ("id" in message or "method" in message)
        ):
            bad.append(line)
    assert not bad, f"lines on standard output that aren't JSON-RPC messages: {bad[:5]}"
    assert not any(b"BLIND-STANDIN" in line for line in raw_run["stdout"])


def test_mcp_srs_118_log_messages_go_to_standard_error(raw_run):
    """MELLON-MCP-SRS-118: the MCP server shall write log messages to standard error.

    The serving line, a log message, is on standard error and not on standard output.
    """
    assert SERVING in raw_run["stderr"], f"no serving line on standard error: {raw_run['stderr'][-600:]!r}"
    assert not any(SERVING.encode() in line for line in raw_run["stdout"]), "serving line on standard output"


# ---------------------------------------------------------------------------
# Offered tools: MELLON-MCP-SRS-34 to -38
# ---------------------------------------------------------------------------


def spec_table(n: int) -> list[tuple[str, ...]]:
    """The body rows of MELLON-MCP-SRS-n's table, backticks removed."""
    tree = etree.parse(str(SPEC_OBJECTS / f"MELLON-MCP-SRS-{n}.xml"))
    rows = []
    for row in tree.iterfind(".//table/row"):
        if row.get("header") == "true":
            continue
        rows.append(tuple("".join(c.itertext()).strip().strip("`") for c in row.iterfind("cell")))
    return rows


def describe_schema(schema: dict, defs: dict) -> str:
    """A parameter's JSON schema in the tables' words, such as 'array of strings or null'."""
    if "$ref" in schema:
        schema = defs[schema["$ref"].rsplit("/", 1)[-1]]
    if "anyOf" in schema:
        parts = [describe_schema(s, defs) for s in schema["anyOf"]]
        others = [p for p in parts if p != "null"]
        if "null" in parts and len(others) == 1:
            return others[0] + " or null"
        return " | ".join(parts)
    if "enum" in schema:
        return "one of " + ", ".join(str(v) for v in schema["enum"])
    kind = schema.get("type")
    if isinstance(kind, list):
        others = [k for k in kind if k != "null"]
        base = describe_schema({**schema, "type": others[0]}, defs)
        return base + " or null" if "null" in kind else base
    if kind == "array":
        items = schema.get("items")
        return "array of " + describe_schema(items, defs) + "s" if items else "array"
    if kind == "object":
        extra = schema.get("additionalProperties")
        return "object of " + describe_schema(extra, defs) + "s" if isinstance(extra, dict) and extra else "object"
    return kind or "any"


def type_matches(table: str, offered: str) -> bool:
    if table == offered:
        return True
    a, b = re.fullmatch(r"one of (.+)", table), re.fullmatch(r"one of (.+)", offered)
    return bool(a and b) and {s.strip() for s in a.group(1).split(",")} == {s.strip() for s in b.group(1).split(",")}


def same_value(a, b) -> bool:
    return type(a) is type(b) and a == b


def table_problems(n: int, tools: dict[str, mcp.types.Tool]) -> list[str]:
    problems = []
    for tool, param, typ, required, default in spec_table(n):
        offered = tools.get(tool)
        if offered is None:
            problems.append(f"{tool}: not offered")
            continue
        schema = offered.inputSchema
        props = schema.get("properties") or {}
        req = set(schema.get("required") or [])
        if param == "(none)":
            if props:
                problems.append(f"{tool}: offers {sorted(props)}, the table says no parameter")
            continue
        prop = props.get(param)
        if prop is None:
            problems.append(f"{tool}.{param}: not offered")
            continue
        got = describe_schema(prop, schema.get("$defs") or {})
        if not type_matches(typ, got):
            problems.append(f"{tool}.{param}: type {got!r}, the table says {typ!r}")
        if (param in req) != (required == "yes"):
            problems.append(f"{tool}.{param}: required is {param in req}, the table says {required!r}")
        if default == NO_VALUE:
            if "default" in prop:
                problems.append(f"{tool}.{param}: default {prop['default']!r}, the table says none")
        elif "default" not in prop or not same_value(prop["default"], json.loads(default)):
            problems.append(f"{tool}.{param}: default {prop.get('default', NO_DEFAULT)!r}, the table says {default}")
    return problems


def test_mcp_srs_34_offers_the_read_tools_of_the_table(tools_session):
    """MELLON-MCP-SRS-34: the MCP server shall offer as read tools the tools and
    parameters in this requirement's table: each row's tool and parameter, with its
    type, whether it is required, and its default."""
    problems = table_problems(34, tools_session["tools"])
    assert not problems, "\n".join(problems)


def test_mcp_srs_35_offers_the_write_tools_of_the_table(tools_session):
    """MELLON-MCP-SRS-35: the MCP server shall offer as write tools the tools and
    parameters in this requirement's table. Array and object types are compared with
    their item types: 'array of strings', 'array of objects', 'object of strings'."""
    problems = table_problems(35, tools_session["tools"])
    assert not problems, "\n".join(problems)


def test_mcp_srs_36_offers_the_git_review_and_export_tools_of_the_table(tools_session):
    """MELLON-MCP-SRS-36: the MCP server shall offer the git, review and export tools and
    parameters in this requirement's table."""
    problems = table_problems(36, tools_session["tools"])
    assert not problems, "\n".join(problems)


def test_mcp_srs_37_offers_no_tool_or_parameter_outside_the_tables(tools_session):
    """MELLON-MCP-SRS-37: the MCP server shall offer no tool and no parameter other than
    those in the tables of MELLON-MCP-SRS-34, -35 and -36.

    Each offered tool is in exactly one table, and each of its parameters is a row under
    it there.
    """
    tables: dict[str, set[int]] = {}
    params: dict[str, set[str]] = {}
    for n in (34, 35, 36):
        for tool, param, *_ in spec_table(n):
            tables.setdefault(tool, set()).add(n)
            if param != "(none)":
                params.setdefault(tool, set()).add(param)
    problems = []
    for name, tool in tools_session["tools"].items():
        if len(tables.get(name, set())) != 1:
            problems.append(f"{name}: in the tables of {sorted(tables.get(name, set()))}")
        extra = set(tool.inputSchema.get("properties") or {}) - params.get(name, set())
        if extra:
            problems.append(f"{name}: parameters outside the table {sorted(extra)}")
    assert not problems, "\n".join(problems)


def test_mcp_srs_38_offers_each_tool_and_parameter_of_the_previous_release(tools_session):
    """MELLON-MCP-SRS-38: the MCP server should offer each tool, parameter, parameter type
    and parameter default that the previous release offered, with each optional
    parameter still optional.

    Compared with list_tools of 0.3.0, recorded in RELEASE_0_3_0_TOOLS. Result fields and
    error_type values aren't compared here.
    """
    previous = json.loads(RELEASE_0_3_0_TOOLS)
    current = {name: t.inputSchema for name, t in tools_session["tools"].items()}
    problems = []
    for tool, params in previous.items():
        schema = current.get(tool)
        if schema is None:
            problems.append(f"{tool}: no longer offered")
            continue
        props = schema.get("properties") or {}
        req = set(schema.get("required") or [])
        for param, (typ, was_required, default) in params.items():
            prop = props.get(param)
            if prop is None:
                problems.append(f"{tool}.{param}: no longer offered")
                continue
            got = describe_schema(prop, schema.get("$defs") or {})
            if got != typ:
                problems.append(f"{tool}.{param}: type {got!r}, was {typ!r}")
            if not was_required and param in req:
                problems.append(f"{tool}.{param}: became required")
            if not same_value(prop.get("default", NO_DEFAULT), default):
                problems.append(f"{tool}.{param}: default {prop.get('default', NO_DEFAULT)!r}, was {default!r}")
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# Read-only server: MELLON-MCP-SRS-98 to -101
# ---------------------------------------------------------------------------


def read_only_calls(figure: str, out_pdf: str) -> dict[str, tuple[str, object]]:
    """A call valid against the rich fixture for each of the 29 refused tools."""
    s = "VCU3-SRS"
    return {
        "new_object": ("new_object", {"doc_id": s, "description": "x"}),
        "set_attribute": ("set_attribute", {"doc_id": s, "obj_id": "VCU3-SRS-2", "name": "status",
                                            "value": "Reviewed"}),
        "edit_description": ("edit_description", {"doc_id": s, "obj_id": "VCU3-SRS-2", "text": "x"}),
        "update_object": ("update_object", {"doc_id": s, "obj_id": "VCU3-SRS-11", "description": "x"}),
        "copy_object": ("copy_object", {"doc_id": s, "obj_id": "VCU3-SRS-12"}),
        "bulk_set_attribute": ("bulk_set_attribute", {"doc_id": s, "obj_ids": ["VCU3-SRS-12"], "name": "priority",
                                                      "value": "Could"}),
        "set_figure": ("set_figure", {"doc_id": s, "obj_id": "VCU3-SRS-2", "source_path": figure}),
        "clear_figure": ("clear_figure", {"doc_id": s, "obj_id": "VCU3-SRS-13"}),
        "set_table": ("set_table", {"doc_id": s, "obj_id": "VCU3-SRS-17", "table": TABLE}),
        "clear_table": ("clear_table", {"doc_id": s, "obj_id": "VCU3-SRS-16"}),
        "edit_metadata": ("edit_metadata", {"doc_id": s, "field": "title", "value": "x"}),
        "tag_issue": ("tag_issue", {"doc_id": s, "issue": "2"}),
        "add_parent_doc": ("add_parent_doc", {"doc_id": "VCU3-STS", "parent_doc": "VCU3-SyRS"}),
        "remove_parent_doc": ("remove_parent_doc", {"doc_id": s, "parent_doc": "VCU3-SyRS"}),
        "add_attr_option": ("add_attr_option", {"doc_id": s, "attr_name": "status", "option": "Obsolete"}),
        "define_attribute": ("define_attribute", {"doc_id": s, "name": "blind-attr", "kind": "text"}),
        "change_attribute_required": ("change_attribute_required", {"doc_id": s, "name": "priority",
                                                                    "required": True}),
        "delete_attribute": ("delete_attribute", {"doc_id": s, "name": "rationale"}),
        "remove_attr_option": ("remove_attr_option", {"doc_id": s, "attr_name": "priority", "option": "Won't"}),
        "manage_view": ("manage_view", {"doc_id": s, "action": "create", "name": "Read-only view"}),
        "create_document": ("create_document", {"doc_id": "BLIND-RO"}),
        "move_object": ("move_object", {"doc_id": s, "obj_id": "VCU3-SRS-10", "parent_id": "VCU3-SRS-0"}),
        "reorder_outline": ("reorder_outline", lambda srv: {"doc_id": s, "ordering": reversed_outline(srv, s)}),
        "add_link": ("add_link", {"source_doc": s, "source_id": "VCU3-SRS-10", "target_doc": "VCU3-SyRS",
                                  "target_id": "VCU3-SyRS-42", "link_type": "satisfies"}),
        "remove_link": ("remove_link", {"source_doc": s, "source_id": "VCU3-SRS-1", "target_doc": "VCU3-SyRS",
                                        "target_id": "VCU3-SyRS-42"}),
        "delete_object": ("delete_object", {"doc_id": s, "obj_id": "VCU3-SRS-12"}),
        "ack_suspects": ("ack_suspects", {"doc_id": s}),
        "ack_suspects_dry_run": ("ack_suspects", {"doc_id": s, "dry_run": True}),
        "commit_changes": ("commit_changes", {"message": "Blind commit", "paths": ["VCU3-STS/objects/VCU3-STS-1.xml"]}),
        "export_document": ("export_document", {"doc_id": s, "out_path": out_pdf}),
    }


READ_ONLY_CASES = list(read_only_calls("x.svg", "x.pdf"))


@pytest.fixture(scope="module")
def read_only_run(tmp_path_factory, rich_template, ident_home, neutral_dir):
    """Each refused tool called on a --read-only server, with every file under the root,
    .git included, hashed before and after each call. VCU3-STS-1 is left modified so
    that commit_changes has something to commit."""
    base = tmp_path_factory.mktemp("read-only")
    env0 = child_env(ident_home)
    root = clone_of(rich_template, base / "repo")
    append_to_description(root / "VCU3-STS" / "objects" / "VCU3-STS-1.xml", "Modified by hand.")
    figure = base / "figure.svg"
    write_lf(figure, SVG)
    out_pdf = base / "read-only.pdf"
    env = child_env(ident_home, **standin_programs(base / "standins", base / "logs"))
    runs = {}
    with mcp_server(
        ["--repo-root", root, "--read-only"], env=env, cwd=neutral_dir, stderr_path=base / "stderr.txt"
    ) as srv:
        srv.call("list_documents")
        srv.call("suspects", {"doc_id": "VCU3-SRS"})
        for case, (tool, args) in read_only_calls(str(figure), str(out_pdf)).items():
            if callable(args):
                args = args(srv)
            refs = ref_state(root, env0)
            before = tree_hashes(root, include_git=True)
            res = srv.call(tool, args)
            after = tree_hashes(root, include_git=True)
            runs[case] = {
                "res": res,
                "changed": changed(before, after),
                "refs_changed": ref_state(root, env0) != refs,
                "pdf_written": out_pdf.exists(),
            }
    return {"root": root, "runs": runs}


def test_mcp_srs_98_read_only_option_makes_the_read_only_server(read_only_run, read_only_twins):
    """MELLON-MCP-SRS-98: when started with --read-only, the MCP server shall run as the
    read-only server.

    The installed program started with --read-only refuses set_attribute and the file
    keeps its content; started without it, the same call succeeds.
    """
    run = read_only_run["runs"]["set_attribute"]
    assert failed(run["res"]), f"set_attribute wasn't refused: {show(run['res'])}"
    assert not run["changed"], f"files changed: {sorted(run['changed'])}"
    without = read_only_twins["answers"]["read_write_set_attribute"]
    assert succeeded(without), f"without --read-only, set_attribute failed: {show(without)}"


@pytest.mark.parametrize("case", READ_ONLY_CASES)
def test_mcp_srs_99_read_only_server_refuses_each_changing_tool(case, read_only_run):
    """MELLON-MCP-SRS-99: the read-only server shall refuse each call of a write tool,
    commit_changes or export_document.

    Each call is answered as a failure, and nothing changes: no file under the root or
    in .git, no ref, no exported PDF. ack_suspects is refused with dry_run true too.
    """
    run = read_only_run["runs"][case]
    assert failed(run["res"]), f"not refused: {show(run['res'])}"
    assert not run["changed"], f"files changed: {sorted(run['changed'])}"
    assert not run["refs_changed"], "a ref or HEAD changed"
    assert not run["pdf_written"], "the PDF was written"


@pytest.mark.parametrize("case", READ_ONLY_CASES)
def test_mcp_srs_100_read_only_refusal_returns_permission_error(case, read_only_run):
    """MELLON-MCP-SRS-100: if the read-only server refuses a tool call, the MCP server
    shall return error_type PermissionError, as the agent's client receives it."""
    res = read_only_run["runs"][case]["res"]
    assert failed(res), f"precondition: not refused: {show(res)}"
    p = payload(res)
    assert isinstance(p, dict) and p.get("error_type") == "PermissionError", show(res)


OTHER_TOOL_CALLS = {
    "list_documents": {},
    "show_document": {"doc_id": "VCU3-SRS", "include": ["*"]},
    "search_objects": {"query": "coolant"},
    "show_object": {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1"},
    "inspect_object": {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1"},
    "outline": {"doc_id": "VCU3-SRS"},
    "validate_document": {"doc_id": "VCU3-SRS"},
    "validate_repo": {},
    "repo_health": {},
    "suspects": {},
    "trace": {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1"},
    "incoming_links": {"doc_id": "VCU3-SyRS", "obj_id": "VCU3-SyRS-42"},
    "coverage_report": {"source_doc": "VCU3-SyRS", "target_doc": "VCU3-SRS"},
    "git_history": {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1"},
    "show_object_at": {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1", "commit_ref": "HEAD~2"},
    "compare_document": {"doc_id": "VCU3-SRS"},
    "review_suspect_link": {"source_doc": "VCU3-SRS", "source_id": "VCU3-SRS-15", "target_doc": "VCU3-SyRS",
                            "target_id": "VCU3-SyRS-47"},
}


@pytest.fixture(scope="module")
def read_only_twins(tmp_path_factory, rich_template, ident_home, neutral_dir):
    """The 17 other tools called with the same arguments on two copies of one state,
    one served with --read-only and one without. After those, the server without
    --read-only also makes the set_attribute call that read_only_run makes."""
    base = tmp_path_factory.mktemp("twins")
    env = child_env(ident_home)
    answers = {}
    roots = []
    write = read_only_calls("x.svg", "x.pdf")["set_attribute"]
    for name, extra in (("read_only", ["--read-only"]), ("read_write", [])):
        root = clone_of(rich_template, base / name / "repo")
        append_to_description(root / "VCU3-SRS" / "objects" / "VCU3-SRS-2.xml", "Uncommitted.")
        roots.append(root)
        with mcp_server(
            ["--repo-root", root, *extra], env=env, cwd=neutral_dir, stderr_path=base / f"{name}-stderr.txt"
        ) as srv:
            answers[name] = {tool: srv.call(tool, args) for tool, args in OTHER_TOOL_CALLS.items()}
            if name == "read_write":
                answers["read_write_set_attribute"] = srv.call(*write)
    return {"answers": answers, "roots": roots}


@pytest.mark.parametrize("tool", list(OTHER_TOOL_CALLS))
def test_mcp_srs_101_read_only_server_answers_other_tools_unchanged(tool, read_only_twins):
    """MELLON-MCP-SRS-101: the read-only server shall answer each call of a tool other
    than a write tool, commit_changes or export_document as the MCP server answers
    without --read-only.

    The same call on two copies of one repository state gives the same result, the
    root's path aside.
    """
    ro = read_only_twins["answers"]["read_only"][tool]
    rw = read_only_twins["answers"]["read_write"][tool]
    roots = read_only_twins["roots"]
    assert ro.isError == rw.isError, f"read-only {show(ro)} / read-write {show(rw)}"
    assert without_roots(payload(ro), roots) == without_roots(payload(rw), roots)


# ---------------------------------------------------------------------------
# Other processes and overlapping calls: MELLON-MCP-SRS-104, -105
# ---------------------------------------------------------------------------


def _commit_all(root: Path, env, message: str) -> str:
    git(root, "commit", "-q", "-a", "-m", message, env=env)
    return git(root, "rev-parse", "HEAD", env=env).strip()


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            "same_size_rewrite",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "defect: MELLON-MCP-SRS-104: after VCU3-SRS-1's object file is rewritten with text "
                    "of the same length and its modified time put back, show_object still answers "
                    "'within 200 ms' from before the change"
                ),
            ),
        ),
        "commit",
        "branch_switch",
        "issue_tag",
        "deleted_document",
    ],
)
def test_mcp_srs_104_second_call_answers_from_the_changed_state(
    change, tmp_path, plain_template, ident_home, neutral_dir
):
    """MELLON-MCP-SRS-104: when files in the folder to serve, the latest commit, a branch
    or a tag change between two tool calls, the MCP server shall answer the second call
    from the changed state.

    The changes, made from outside the server: an object file rewritten with text of the
    same length and its modified time put back; a commit; a switch to another branch;
    a new issue tag; a deleted document folder.
    """
    env = child_env(ident_home)
    root = clone_of(plain_template, tmp_path / "repo")
    srs1 = root / "VCU3-SRS" / "objects" / "VCU3-SRS-1.xml"
    srs2 = root / "VCU3-SRS" / "objects" / "VCU3-SRS-2.xml"
    other_commit = None
    if change == "commit":
        append_to_description(srs1, "Committed from outside.")
    elif change == "branch_switch":
        git(root, "checkout", "-q", "-b", "other", env=env)
        append_to_description(srs2, "Changed on the other branch.")
        other_commit = _commit_all(root, env, "Change SRS-2 on another branch")
        git(root, "checkout", "-q", "master", env=env)

    with mcp_server(["--repo-root", root], env=env, cwd=neutral_dir, stderr_path=tmp_path / "stderr.txt") as srv:
        if change == "same_size_rewrite":
            ask = ("show_object", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1"})
            first = srv.call(*ask)
            stat = srs1.stat()
            data = srs1.read_bytes()
            assert b"within 200 ms" in data, "setup: unexpected fixture text"
            srs1.write_bytes(data.replace(b"within 200 ms", b"within 300 ms"))
            os.utime(srs1, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            assert srs1.stat().st_size == stat.st_size and srs1.stat().st_mtime_ns == stat.st_mtime_ns
            second = srv.call(*ask)
            assert "within 200 ms" in result_text(first), f"precondition: {show(first)}"
            assert "within 300 ms" in result_text(second), f"answered from the old file: {show(second)}"
        elif change == "commit":
            ask = ("git_history", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1"})
            first = srv.call(*ask)
            new = _commit_all(root, env, "Commit SRS-1 from outside the server")
            second = srv.call(*ask)
            assert new[:7] not in result_text(first), f"precondition: {show(first)}"
            assert new[:7] in result_text(second), f"no new commit {new[:7]} in {show(second)}"
        elif change == "branch_switch":
            ask = ("git_history", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-2"})
            first = srv.call(*ask)
            git(root, "checkout", "-q", "other", env=env)
            second = srv.call(*ask)
            assert other_commit[:7] not in result_text(first), f"precondition: {show(first)}"
            assert other_commit[:7] in result_text(second), f"no branch commit in {show(second)}"
        elif change == "issue_tag":
            first = srv.call("list_documents")
            git(root, "tag", "VCU3-SRS-issue-2", env=env)
            second = srv.call("list_documents")
            issue = {d["doc_id"]: d.get("current_issue") for d in payload(second)}
            assert {d["doc_id"]: d.get("current_issue") for d in payload(first)}["VCU3-SRS"] == "1", "precondition"
            assert issue["VCU3-SRS"] == "2", f"current_issue {issue['VCU3-SRS']!r} after tagging issue 2"
        else:
            first = srv.call("list_documents")
            shutil.rmtree(root / "VCU3-STS")
            second = srv.call("list_documents")
            assert set(doc_ids(first)) == FIXTURE_DOCS, f"precondition: {show(first)}"
            assert set(doc_ids(second)) == {"VCU3-SRS", "VCU3-SyRS"}, show(second)


def _manifest_refs(root: Path, doc_id: str) -> list[str]:
    tree = etree.parse(str(root / doc_id / f"{doc_id}.xml"))
    return [ref.get("id") for ref in tree.iterfind(".//outline//ref")]


def _number(obj_id: str) -> int:
    return int(obj_id.rsplit("-", 1)[1])


def test_mcp_srs_105_overlapping_new_object_calls_run_as_one_after_the_other(
    tmp_path, plain_template, ident_home, neutral_dir
):
    """MELLON-MCP-SRS-105: when two tool calls overlap, the MCP server shall give each call
    the result and file changes that running the calls one after the other gives.

    Two new_object calls for VCU3-SRS sent before either is answered: the objects get
    different IDs, the manifest lists both, and the results and files equal those of the
    same calls made one after the other on a copy.
    """
    env = child_env(ident_home)
    conc = clone_of(plain_template, tmp_path / "concurrent")
    seq = clone_of(plain_template, tmp_path / "sequential")
    calls = {
        "A": ("new_object", {"doc_id": "VCU3-SRS", "description": "Overlap A."}),
        "B": ("new_object", {"doc_id": "VCU3-SRS", "description": "Overlap B."}),
    }
    before = set(tree_hashes(conc, include_git=False))
    with mcp_server(["--repo-root", conc], env=env, cwd=neutral_dir, stderr_path=tmp_path / "c.txt") as srv:
        results = dict(zip(calls, srv.call_concurrently(list(calls.values()))))
    new_files = sorted(set(tree_hashes(conc, include_git=False)) - before)
    new_objects = {
        Path(f).stem: (conc / f).read_text(encoding="utf-8")
        for f in new_files
        if re.fullmatch(r"VCU3-SRS/objects/VCU3-SRS-\d+\.xml", f)
    }
    assert len(new_objects) == 2, f"expected two new objects with different IDs, got {sorted(new_objects)}"
    refs = _manifest_refs(conc, "VCU3-SRS")
    assert set(new_objects) <= set(refs), f"the manifest lists {refs}"
    owner = {key: next(i for i, t in new_objects.items() if f"Overlap {key}." in t) for key in calls}
    order = sorted(calls, key=lambda key: _number(owner[key]))

    with mcp_server(["--repo-root", seq], env=env, cwd=neutral_dir, stderr_path=tmp_path / "s.txt") as srv:
        sequential = {key: srv.call(*calls[key]) for key in order}
    roots = [conc, seq]
    for key in calls:
        assert without_roots(payload(results[key]), roots) == without_roots(payload(sequential[key]), roots), (
            f"call {key}: overlapping {show(results[key])} / one after the other {show(sequential[key])}"
        )
    assert tree_hashes(conc, include_git=False) == tree_hashes(seq, include_git=False), "the files differ"


def test_mcp_srs_105_overlapping_write_and_compare_run_as_one_after_the_other(
    tmp_path, plain_template, ident_home, neutral_dir
):
    """MELLON-MCP-SRS-105: when two tool calls overlap, the MCP server shall give each call
    the result and file changes that running the calls one after the other gives.

    set_attribute and compare_document of HEAD with the working copy sent before either
    is answered: set_attribute's result and the files equal a sequential run's, and
    compare_document's result equals the one before or the one after set_attribute.
    """
    env = child_env(ident_home)
    conc = clone_of(plain_template, tmp_path / "concurrent")
    seq = clone_of(plain_template, tmp_path / "sequential")
    write = ("set_attribute", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-2", "name": "status", "value": "Reviewed"})
    compare = ("compare_document", {"doc_id": "VCU3-SRS", "from_ref": "HEAD", "to_ref": "working copy"})
    with mcp_server(["--repo-root", conc], env=env, cwd=neutral_dir, stderr_path=tmp_path / "c.txt") as srv:
        written, compared = srv.call_concurrently([write, compare])
    with mcp_server(["--repo-root", seq], env=env, cwd=neutral_dir, stderr_path=tmp_path / "s.txt") as srv:
        compare_before = srv.call(*compare)
        written_seq = srv.call(*write)
        compare_after = srv.call(*compare)
    roots = [conc, seq]
    assert succeeded(written_seq), f"precondition: {show(written_seq)}"
    assert without_roots(payload(written), roots) == without_roots(payload(written_seq), roots), show(written)
    options = [without_roots(payload(r), roots) for r in (compare_before, compare_after)]
    assert without_roots(payload(compared), roots) in options, f"compare_document gave {show(compared)}"
    assert tree_hashes(conc, include_git=False) == tree_hashes(seq, include_git=False), "the files differ"


# ---------------------------------------------------------------------------
# Paths outside the repository: MELLON-MCP-SRS-112
# ---------------------------------------------------------------------------


PLANTED = "PLANTED-OUTSIDE-TEXT"
UP = "../../../outside/objects/outside-1"  # from <doc>/objects/ to the planted object file


@pytest.fixture(scope="module")
def outside_case(tmp_path_factory, plain_template, ident_home, neutral_dir):
    """A clone at <case>/repo beside a planted document and files at <case>/outside."""
    case = tmp_path_factory.mktemp("outside-case")
    logs = tmp_path_factory.mktemp("outside-logs")
    env = child_env(ident_home)
    root = clone_of(plain_template, case / "repo")
    outside = case / "outside"
    manifest = MINI_MANIFEST.format(doc_id="outside", title=PLANTED)
    write_lf(outside / "outside.xml", manifest)
    write_lf(case / "outside.xml", manifest)
    write_lf(outside / "objects" / "outside-1.xml", MINI_OBJECT.format(obj_id="outside-1", text=PLANTED))
    write_lf(outside / "planted-view.xml",
             f'<?xml version="1.0" encoding="UTF-8"?>\n<view name="{PLANTED}">\n  <attribute name="status"/>\n</view>\n')
    write_lf(outside / "sentinel.txt", PLANTED + "\n")
    write_lf(outside / "HEAD", git(root, "rev-parse", "HEAD~1", env=env).strip() + "\n")
    with mcp_server(["--repo-root", root], env=env, cwd=neutral_dir, stderr_path=logs / "stderr.txt") as srv:
        yield {"client": srv, "case": case, "root": root, "outside": outside}


def _other_drive(root: Path) -> str | None:
    if not WINDOWS:
        return None
    own = os.path.splitdrive(str(root))[0].rstrip(":").upper()
    for letter in string.ascii_uppercase:
        if letter != own and os.path.exists(f"{letter}:/"):
            return letter
    return None


OUTSIDE_CASES = {
    "show_document_doc_id_parent_folder": ("show_document", {"doc_id": "../outside"}),
    "show_document_doc_id_absolute": ("show_document", {"doc_id": "<OUTSIDE>"}),
    "show_object_obj_id_parent_folder": ("show_object", {"doc_id": "VCU3-SRS", "obj_id": UP}),
    "show_object_obj_id_absolute": ("show_object", {"doc_id": "VCU3-SRS", "obj_id": "<OUTSIDE>/objects/outside-1"}),
    "new_object_doc_id_parent_folder": ("new_object", {"doc_id": "../outside", "description": "x"}),
    "new_object_parent_id_parent_folder": ("new_object", {"doc_id": "VCU3-SRS", "parent_id": UP}),
    "set_attribute_obj_id_parent_folder": ("set_attribute", {"doc_id": "VCU3-SRS", "obj_id": UP,
                                                             "name": "type", "value": "Heading"}),
    "set_attribute_obj_id_absolute": ("set_attribute", {"doc_id": "VCU3-SRS",
                                                        "obj_id": "<OUTSIDE>/objects/outside-1",
                                                        "name": "type", "value": "Heading"}),
    "edit_description_doc_id_absolute": ("edit_description", {"doc_id": "<OUTSIDE>", "obj_id": "outside-1",
                                                              "text": "Overwritten."}),
    "delete_object_obj_id_parent_folder": ("delete_object", {"doc_id": "VCU3-SRS", "obj_id": UP, "force": True}),
    "add_link_source_doc_parent_folder": ("add_link", {"source_doc": "../outside", "source_id": "outside-1",
                                                       "target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-42",
                                                       "link_type": "satisfies"}),
    "add_link_source_id_parent_folder": ("add_link", {"source_doc": "VCU3-SRS", "source_id": UP,
                                                      "target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-42",
                                                      "link_type": "satisfies"}),
    "add_link_target_doc_parent_folder": ("add_link", {"source_doc": "VCU3-SRS", "source_id": "VCU3-SRS-10",
                                                       "target_doc": "../outside", "target_id": "outside-1",
                                                       "link_type": "satisfies"}),
    "add_link_target_id_parent_folder": ("add_link", {"source_doc": "VCU3-SRS", "source_id": "VCU3-SRS-10",
                                                      "target_doc": "VCU3-SyRS", "target_id": UP,
                                                      "link_type": "satisfies"}),
    "create_document_doc_id_parent_folder": ("create_document", {"doc_id": "../outside-new"}),
    "create_document_doc_id_absolute": ("create_document", {"doc_id": "<CASE>/outside-abs"}),
    "manage_view_slug_delete": ("manage_view", {"doc_id": "VCU3-SRS", "action": "delete",
                                                "slug": "../../../outside/planted-view"}),
    "manage_view_slug_rename": ("manage_view", {"doc_id": "VCU3-SRS", "action": "rename",
                                                "slug": "../../../outside/planted-view", "name": "Renamed"}),
    "manage_view_slug_edit": ("manage_view", {"doc_id": "VCU3-SRS", "action": "edit",
                                              "slug": "../../../outside/planted-view", "visible_attrs": ["type"]}),
    "git_history_doc_id_parent_folder": ("git_history", {"doc_id": "../outside", "obj_id": "outside-1"}),
    "show_object_at_commit_ref_parent_folder": ("show_object_at", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1",
                                                                   "commit_ref": "../../outside/HEAD"}),
    "show_object_at_commit_ref_absolute": ("show_object_at", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1",
                                                              "commit_ref": "<OUTSIDE>/HEAD"}),
    "compare_document_from_ref_parent_folder": ("compare_document", {"doc_id": "VCU3-SRS",
                                                                     "from_ref": "../../outside/HEAD"}),
    "compare_document_to_ref_parent_folder": ("compare_document", {"doc_id": "VCU3-SRS", "from_ref": "HEAD~1",
                                                                   "to_ref": "../../outside/HEAD"}),
    "review_suspect_link_target_doc_parent_folder": ("review_suspect_link", {
        "source_doc": "VCU3-SRS", "source_id": "VCU3-SRS-1", "target_doc": "../outside", "target_id": "outside-1"}),
    "ack_suspects_link_key_source_id_parent_folder": ("ack_suspects", {"doc_id": "VCU3-SRS", "link_keys": [
        {"source_id": UP, "target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-42"}]}),
    "ack_suspects_link_key_target_doc_parent_folder": ("ack_suspects", {"doc_id": "VCU3-SRS", "link_keys": [
        {"source_id": "VCU3-SRS-1", "target_doc": "../outside", "target_id": "outside-1"}]}),
    "commit_changes_paths_parent_folder": ("commit_changes", {"message": "Blind commit",
                                                              "paths": ["../outside/sentinel.txt"]}),
    "commit_changes_paths_absolute": ("commit_changes", {"message": "Blind commit",
                                                         "paths": ["<OUTSIDE>/sentinel.txt"]}),
    "create_document_doc_id_other_drive": ("create_document", {"doc_id": "<DRIVE>:/blind-mcp-<UUID>"}),
}


# Values the call can succeed with without resolving them to a file: a link's stored
# target ID and ack_suspects' link keys, which are only matched against existing links.
# For these, whether succeeding counts as "accepting a path" is open (see the report),
# so only the effects on files outside and on the answer are checked.
DATA_NOT_PATH = {
    "add_link_target_id_parent_folder",
    "ack_suspects_link_key_source_id_parent_folder",
    "ack_suspects_link_key_target_doc_parent_folder",
}


def _fill(value, mapping: dict[str, str]):
    if isinstance(value, dict):
        return {k: _fill(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, mapping) for v in value]
    if isinstance(value, str):
        for key, replacement in mapping.items():
            value = value.replace(key, replacement)
    return value


@pytest.mark.parametrize("case", list(OUTSIDE_CASES))
def test_mcp_srs_112_refuses_paths_outside_the_repository(case, outside_case):
    """MELLON-MCP-SRS-112: the MCP server shall accept a path to a file outside the
    repository only in set_figure's source_path and export_document's out_path.

    Each ID, name, ref or path parameter given a value that resolves outside the
    repository (a parent folder, an absolute path, another drive): no file outside the
    repository is created, changed or deleted, no planted text comes back, and the call
    fails, except for the DATA_NOT_PATH cases. Whether a file outside is opened is not
    checked.
    """
    tool, args = OUTSIDE_CASES[case]
    drive = _other_drive(outside_case["root"])
    if "<DRIVE>" in json.dumps(args) and drive is None:
        pytest.skip("needs a second drive letter (Windows)")
    marker = f"blind-mcp-{uuid.uuid4().hex[:8]}"
    args = _fill(args, {
        "<OUTSIDE>": outside_case["outside"].as_posix(),
        "<CASE>": outside_case["case"].as_posix(),
        "<DRIVE>": drive or "",
        "blind-mcp-<UUID>": marker,
    })
    case_dir = outside_case["case"]
    before = tree_hashes(case_dir, include_git=True, skip=("repo",))
    res = outside_case["client"].call(tool, args)
    after = tree_hashes(case_dir, include_git=True, skip=("repo",))
    other_drive_folder = Path(f"{drive}:/{marker}") if drive else None
    try:
        assert not changed(before, after), f"files outside the repository changed: {sorted(changed(before, after))}"
        assert other_drive_folder is None or not other_drive_folder.exists(), f"created {other_drive_folder}"
        assert PLANTED not in result_text(res), f"planted text came back: {show(res)}"
        if case not in DATA_NOT_PATH:
            assert failed(res), f"accepted: {show(res)}"
    finally:
        if other_drive_folder is not None and other_drive_folder.exists():
            shutil.rmtree(other_drive_folder, ignore_errors=True)


# MELLON-MCP-SRS blind tests
from tests.mcp_stdio_support import fold, rel_paths  # noqa: E402


# ---------------------------------------------------------------------------
# Committing: MELLON-MCP-SRS-79, -81, -87, -91, -94
# ---------------------------------------------------------------------------


def _index_state(root: Path, env) -> tuple[str, str, str]:
    return (
        ref_state(root, env),
        git(root, "ls-files", "--stage", env=env),
        git(root, "diff-index", "--cached", "--name-status", "HEAD", env=env),
    )


def test_mcp_srs_79_no_commit_message_leaves_commits_branches_and_index(
    tmp_path, rich_template, ident_home, neutral_dir
):
    """MELLON-MCP-SRS-79: when a write tool call gives no commit_message, or gives null,
    the MCP server shall leave the commits, branches and git index unchanged.

    With VCU3-STS-1 staged beforehand: the refs, HEAD, the index entries and what is
    staged are the same after each call, and VCU3-STS-1 is still staged.
    """
    env = child_env(ident_home)
    root = clone_of(rich_template, tmp_path / "repo")
    append_to_description(root / "VCU3-STS" / "objects" / "VCU3-STS-1.xml", "Staged by hand.")
    git(root, "add", "VCU3-STS/objects/VCU3-STS-1.xml", env=env)
    before = _index_state(root, env)
    assert "VCU3-STS/objects/VCU3-STS-1.xml" in before[2], "setup: nothing staged"
    base_text = "The VCU shall sample the coolant temperature at a rate of at least {} Hz."
    with mcp_server(["--repo-root", root], env=env, cwd=neutral_dir, stderr_path=tmp_path / "stderr.txt") as srv:
        omitted = srv.call("edit_description", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-2",
                                                "text": base_text.format(30)})
        after_omitted = _index_state(root, env)
        null = srv.call("edit_description", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-2",
                                             "text": base_text.format(40), "commit_message": None})
        after_null = _index_state(root, env)
    assert succeeded(omitted) and succeeded(null), f"precondition: {show(omitted)} / {show(null)}"
    assert after_omitted == before, "commits, branches or index changed without commit_message"
    assert after_null == before, "commits, branches or index changed with commit_message null"


@pytest.fixture(scope="module")
def commit_run(tmp_path_factory, rich_template, ident_home, neutral_dir):
    """edit_description with commit_message, while VCU3-STS-1 is modified and VCU3-SyRS-42
    staged by someone else."""
    base = tmp_path_factory.mktemp("commit")
    env = child_env(ident_home)
    root = clone_of(rich_template, base / "repo")
    append_to_description(root / "VCU3-STS" / "objects" / "VCU3-STS-1.xml", "Modified by hand.")
    append_to_description(root / "VCU3-SyRS" / "objects" / "VCU3-SyRS-42.xml", "Staged by hand.")
    git(root, "add", "VCU3-SyRS/objects/VCU3-SyRS-42.xml", env=env)
    head_before = git(root, "rev-parse", "HEAD", env=env).strip()
    with mcp_server(["--repo-root", root], env=env, cwd=neutral_dir, stderr_path=base / "stderr.txt") as srv:
        res = srv.call(
            "edit_description",
            {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-2", "commit_message": "Tighten SRS-2",
             "text": "The VCU shall sample the coolant temperature at a rate of at least 50 Hz."},
        )
    head_after = git(root, "rev-parse", "HEAD", env=env).strip()
    raw_commit = git(root, "cat-file", "commit", "HEAD", env=env)
    return {
        "root": root,
        "env": env,
        "res": res,
        "head_before": head_before,
        "head_after": head_after,
        "parent": git(root, "rev-parse", "HEAD^", env=env).strip(),
        "files": set(git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", env=env).split()),
        "message": raw_commit.split("\n\n", 1)[1],
        "unstaged": set(git(root, "diff-files", "--name-only", env=env).split()),
        "staged": set(git(root, "diff-index", "--cached", "--name-only", "HEAD", env=env).split()),
    }


def test_mcp_srs_81_commits_exactly_affected_files_with_the_message(commit_run):
    """MELLON-MCP-SRS-81: when a write tool call with a commit_message succeeds, the MCP
    server shall commit exactly the files in affected_files, with commit_message as the
    message.

    Another file modified and another staged stay out of the commit and keep their
    states.
    """
    run = commit_run
    assert succeeded(run["res"]), f"precondition: {show(run['res'])}"
    assert run["parent"] == run["head_before"], "the new commit isn't on top of the old HEAD"
    affected = rel_paths(run["root"], payload(run["res"])["affected_files"])
    assert {fold(f) for f in run["files"]} == affected, f"committed {run['files']}, affected {affected}"
    assert run["message"].startswith("Tighten SRS-2"), f"message {run['message']!r}"
    assert "VCU3-STS/objects/VCU3-STS-1.xml" in run["unstaged"], "the modified file isn't modified any more"
    assert "VCU3-STS/objects/VCU3-STS-1.xml" not in run["staged"], "the modified file got staged"
    assert "VCU3-SyRS/objects/VCU3-SyRS-42.xml" in run["staged"], "the staged file isn't staged any more"
    assert "VCU3-SyRS/objects/VCU3-SyRS-42.xml" not in run["files"], "the staged file went into the commit"


def test_mcp_srs_81_commits_with_no_git_program_installed(tmp_path, rich_template, ident_home, neutral_dir):
    """MELLON-MCP-SRS-81: when a write tool call with a commit_message succeeds, the MCP
    server shall commit exactly the files in affected_files, with commit_message as the
    message.

    Started with no git program on PATH: the commit is still made, with exactly the
    affected files.
    """
    root = clone_of(rich_template, tmp_path / "repo")
    full_env = child_env(ident_home)
    folders = [str(Path(sys.executable).parent)]
    if WINDOWS:
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        folders += [str(Path(system_root) / "System32"), system_root]
    path = os.pathsep.join(folders)
    assert shutil.which("git", path=path) is None, "setup: git is still on the reduced PATH"
    head_before = git(root, "rev-parse", "HEAD", env=full_env).strip()
    with mcp_server(
        ["--repo-root", root], env={**full_env, "PATH": path}, cwd=neutral_dir, stderr_path=tmp_path / "stderr.txt"
    ) as srv:
        res = srv.call("set_attribute", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-2", "name": "status",
                                         "value": "Reviewed", "commit_message": "Review SRS-2 without git"})
    assert succeeded(res), show(res)
    assert git(root, "rev-parse", "HEAD^", env=full_env).strip() == head_before, f"no new commit: {show(res)}"
    files = set(git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", env=full_env).split())
    assert {fold(f) for f in files} == rel_paths(root, payload(res)["affected_files"]), files


def test_mcp_srs_87_returns_commit_short_hash_and_message(commit_run):
    """MELLON-MCP-SRS-87: when the MCP server commits for a commit_message, it shall
    return commit with the commit's short_hash and commit_message."""
    run = commit_run
    commit = payload(run["res"]).get("commit")
    assert isinstance(commit, dict), f"no commit in {show(run['res'])}"
    short = commit.get("short_hash")
    assert isinstance(short, str) and short and run["head_after"].startswith(short), (
        f"short_hash {short!r}, HEAD {run['head_after']}"
    )
    assert commit.get("commit_message") == run["message"], (
        f"commit_message {commit.get('commit_message')!r}, the commit's {run['message']!r}"
    )


@pytest.fixture(scope="module")
def commit_failure_runs(tmp_path_factory, rich_template, ident_home, neutral_dir):
    """edit_description with commit_message while git refuses the commit: during a merge
    in progress, with no git identity, and with user.email but no user.name."""
    runs = {}
    text = "The VCU shall generate a FAULT_OVERTEMP signal within 150 ms of the coolant temperature exceeding 106 degC."
    for kind in ("merge_in_progress", "no_identity", "no_user_name"):
        base = tmp_path_factory.mktemp(kind)
        root = clone_of(rich_template, base / "repo")
        if kind == "merge_in_progress":
            env = child_env(ident_home)
            other = git(root, "rev-parse", "HEAD~1", env=env).strip()
            write_lf(root / ".git" / "MERGE_HEAD", other + "\n")
        else:
            home = base / "home"
            home.mkdir()
            if kind == "no_user_name":
                write_lf(home / ".gitconfig", "[user]\n\temail = blind@example.invalid\n")
            extra = {"APPDATA": str(home / "AppData"), "LOCALAPPDATA": str(home / "Local")} if WINDOWS else {}
            env = child_env(home, **extra)
        head_before = git(root, "rev-parse", "HEAD", env=env).strip()
        with mcp_server(["--repo-root", root], env=env, cwd=neutral_dir, stderr_path=base / "stderr.txt") as srv:
            res = srv.call("edit_description", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1", "text": text,
                                                "commit_message": "Blind commit that git refuses"})
        runs[kind] = {
            "res": res,
            "head_before": head_before,
            "head_after": git(root, "rev-parse", "HEAD", env=env).strip(),
            "object": (root / "VCU3-SRS" / "objects" / "VCU3-SRS-1.xml").read_text(encoding="utf-8"),
            "text": text,
        }
    return runs


COMMIT_REASONS = {
    "merge_in_progress": re.compile(r"(?i)merg"),
    "no_identity": re.compile(r"(?i)user\.name|user\.email|identity|signature|author|committer"),
    "no_user_name": re.compile(r"(?i)user\.name|identity|signature|author|committer"),
}


@pytest.mark.parametrize("kind", list(COMMIT_REASONS))
def test_mcp_srs_91_failed_commit_returns_commit_error_with_reason(kind, commit_failure_runs):
    """MELLON-MCP-SRS-91: if the commit for a commit_message fails, the MCP server shall
    return commit_error with the reason.

    git refuses the commit during a merge in progress, with no git identity, and with
    no user.name; the reason names the merge, or the missing identity.
    """
    res = commit_failure_runs[kind]["res"]
    p = payload(res)
    assert isinstance(p, dict) and "commit_error" in p, f"no commit_error: {show(res)}"
    reason = json.dumps(p["commit_error"]) if not isinstance(p["commit_error"], str) else p["commit_error"]
    assert COMMIT_REASONS[kind].search(reason), f"commit_error doesn't give the reason: {reason!r}"


@pytest.mark.parametrize("kind", list(COMMIT_REASONS))
def test_mcp_srs_94_failed_commit_keeps_the_change_in_the_working_copy(kind, commit_failure_runs):
    """MELLON-MCP-SRS-94: if the commit for a commit_message fails, the MCP server shall
    keep the call's changes in the working copy.

    The object file holds the new text and HEAD hasn't moved.
    """
    run = commit_failure_runs[kind]
    assert run["head_after"] == run["head_before"], "HEAD moved although git refused the commit"
    assert run["text"] in run["object"], "the edited text isn't in the working copy"

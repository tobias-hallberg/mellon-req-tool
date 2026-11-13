"""Tests for the `mellon-check` gate."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pygit2
import pytest

from mellon.check import main
from mellon.editor import add_link

MANIFEST = """\
<?xml version="1.0" encoding="UTF-8"?>
<document id="{id}">
  <metadata><title>{id}</title></metadata>
  {parents}
  <attributes>
    <attribute name="status" kind="enum" required="true">
      <option>Draft</option>
    </attribute>
  </attributes>
  <outline><ref id="{id}-1"/></outline>
</document>
"""

OBJECT = """\
<?xml version="1.0" encoding="UTF-8"?>
<object id="{id}-1">
  <attr name="status">{status}</attr>
  <description>text</description>
</object>
"""


def _write_doc(root: Path, doc_id: str, parents: str = "", status: str = "Draft") -> None:
    (root / doc_id / "objects").mkdir(parents=True)
    (root / doc_id / f"{doc_id}.xml").write_text(
        MANIFEST.format(id=doc_id, parents=parents), encoding="utf-8"
    )
    (root / doc_id / "objects" / f"{doc_id}-1.xml").write_text(
        OBJECT.format(id=doc_id, status=status), encoding="utf-8"
    )


def _commit_all(root: Path, message: str = "c") -> None:
    repo = pygit2.Repository(str(root))
    repo.index.add_all()
    repo.index.write()
    sig = pygit2.Signature("t", "t@x")
    parents = [] if repo.head_is_unborn else [repo.head.target]
    repo.create_commit("HEAD", sig, sig, message, repo.index.write_tree(), parents)


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    """CHILD-1 links to a committed PARENT-1 and carries a correct ack."""
    pygit2.init_repository(str(tmp_path))
    _write_doc(tmp_path, "PARENT")
    _write_doc(tmp_path, "CHILD", parents='<parents><parent doc="PARENT"/></parents>')
    _commit_all(tmp_path, "documents")
    assert add_link(tmp_path / "CHILD", "CHILD-1", "PARENT", "PARENT-1", "satisfies")
    _commit_all(tmp_path, "link")
    return tmp_path


def test_clean_repo_passes(clean_repo: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["--repo-root", str(clean_repo)]) == 0
    assert capsys.readouterr().out == "2 document(s) checked, 0 problem(s)\n"


def test_repo_without_documents_passes(tmp_path: Path, capsys) -> None:
    pygit2.init_repository(str(tmp_path))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(tmp_path)

    assert main(["--repo-root", str(tmp_path)]) == 0
    assert "0 document(s) checked" in capsys.readouterr().out


def test_root_defaults_to_claude_project_dir(
    clean_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(clean_repo))
    assert main([]) == 0
    assert "2 document(s) checked" in capsys.readouterr().out


def test_suspect_link_fails(clean_repo: Path, capsys) -> None:
    parent = clean_repo / "PARENT" / "objects" / "PARENT-1.xml"
    parent.write_text(
        parent.read_text(encoding="utf-8").replace("text", "revised"), encoding="utf-8"
    )
    _commit_all(clean_repo, "revise parent")

    assert main(["--repo-root", str(clean_repo)]) == 1

    out = capsys.readouterr().out
    assert "CHILD: SUSPECT CHILD-1 -> PARENT/PARENT-1" in out
    assert "1 problem(s)" in out


def test_broken_link_fails(clean_repo: Path, capsys) -> None:
    (clean_repo / "PARENT" / "objects" / "PARENT-1.xml").unlink()

    assert main(["--repo-root", str(clean_repo)]) == 1
    assert "CHILD: BROKEN CHILD-1 -> PARENT/PARENT-1" in capsys.readouterr().out


def _add_child_link(root: Path, target_id: str) -> None:
    links = root / "CHILD" / "links" / "CHILD-1.xml"
    links.write_text(links.read_text(encoding="utf-8").replace(
        "</links>",
        f'  <link target-doc="CHILD" target-id="{target_id}" type="refines"/>\n</links>',
    ), encoding="utf-8")


def test_broken_link_within_its_document_is_one_line(clean_repo: Path, capsys) -> None:
    # MELLON-CHECK-SRS-134: the validation error is the link's one line.
    _add_child_link(clean_repo, "CHILD-9")
    _commit_all(clean_repo, "broken link")

    assert main(["--repo-root", str(clean_repo)]) == 1

    out = capsys.readouterr().out
    assert "CHILD: LINK_TARGET_MISSING links/CHILD-1.xml: link target CHILD/CHILD-9" in out
    assert "BROKEN" not in out
    assert "1 problem(s)" in out


def test_link_to_uncommitted_object_of_its_document_keeps_its_line(
    clean_repo: Path, capsys
) -> None:
    # The target exists, so no LINK_TARGET_MISSING: its link line isn't dropped.
    manifest = clean_repo / "CHILD" / "CHILD.xml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace(
        '<ref id="CHILD-1"/>', '<ref id="CHILD-1"/><ref id="CHILD-2"/>'
    ), encoding="utf-8")
    (clean_repo / "CHILD" / "objects" / "CHILD-2.xml").write_text(
        OBJECT.format(id="CHILD", status="Draft").replace("CHILD-1", "CHILD-2"),
        encoding="utf-8",
    )
    _add_child_link(clean_repo, "CHILD-2")

    assert main(["--repo-root", str(clean_repo)]) == 1

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "CHILD-1 -> CHILD/CHILD-2" in line]
    assert len(lines) == 1, out
    assert "1 problem(s)" in out


def test_validation_error_fails(clean_repo: Path, capsys) -> None:
    (clean_repo / "PARENT" / "objects" / "PARENT-1.xml").write_text(
        OBJECT.format(id="PARENT", status="Nonsense"), encoding="utf-8"
    )

    assert main(["--repo-root", str(clean_repo)]) == 1
    assert "PARENT: ATTR_BAD_ENUM" in capsys.readouterr().out


def test_document_that_fails_to_load_fails(clean_repo: Path, capsys) -> None:
    (clean_repo / "CHILD" / "CHILD.xml").write_text("<<<<<<< HEAD", encoding="utf-8")

    assert main(["--repo-root", str(clean_repo)]) == 1

    out = capsys.readouterr().out
    assert "CHILD: LOAD_ERROR" in out
    assert "1 document(s) checked" in out


def test_req_test_fixture_lists_its_suspects(
    tmp_path: Path, clean_worktree, capsys
) -> None:
    src = (Path(__file__).resolve().parents[2] / "req-test").resolve()
    if not (src / ".git").exists():
        pytest.skip(f"req-test repo not available at {src}")
    dst = tmp_path / "req-test"
    shutil.copytree(src, dst)
    clean_worktree(dst)

    assert main(["--repo-root", str(dst)]) == 1

    out = capsys.readouterr().out
    # The fixture was engineered for exactly 6 suspects and no broken links.
    assert out.count("VCU3-SRS: SUSPECT ") == 6
    assert "BROKEN" not in out
    assert "3 document(s) checked, 6 problem(s)" in out


def test_root_outside_git_is_refused(tmp_path: Path, capsys) -> None:
    assert main(["--repo-root", str(tmp_path)]) == 2
    assert capsys.readouterr().err


def test_missing_root_is_refused(tmp_path: Path) -> None:
    assert main(["--repo-root", str(tmp_path / "nope")]) == 2


def test_subfolder_of_a_clone_is_refused(clean_repo: Path) -> None:
    assert main(["--repo-root", str(clean_repo / "CHILD")]) == 2


def test_shallow_clone_is_refused(clean_repo: Path, tmp_path_factory, capsys) -> None:
    shallow = tmp_path_factory.mktemp("shallow") / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", clean_repo.as_uri(), str(shallow)],
        check=True,
    )
    assert main(["--repo-root", str(shallow)]) == 2
    assert "shallow" in capsys.readouterr().err.lower()


# --- written blind from MELLON-CHECK-SRS ---
# Imports for this block (the helpers below are prefixed _blind_).
import datetime
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pytest

from mellon.loader import load_document
from mellon.validator import validate
from tests.conftest import _ensure_req_test_fixture


_BLIND_DEFAULT_ATTRS = (
    ("type", "enum", True, ("Heading", "Requirement", "Information")),
    ("status", "enum", False, ("Draft", "Approved")),
    ("rationale", "text", False, ()),
)


def _blind_manifest(doc_id, *, attrs=_BLIND_DEFAULT_ATTRS, outline=(), parents=(),
                    date="2026-04-21"):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<document id="{doc_id}">',
        "  <metadata>",
        f"    <title>{doc_id}</title>",
        "    <author>tester</author>",
    ]
    if date is not None:
        lines.append(f"    <date>{escape(date)}</date>")
    lines.append("  </metadata>")
    if parents:
        lines.append("  <parents>")
        lines += [f'    <parent doc="{p}"/>' for p in parents]
        lines.append("  </parents>")
    else:
        lines.append("  <parents/>")
    lines.append("  <attributes>")
    for name, kind, required, options in attrs:
        req = "true" if required else "false"
        if options:
            lines.append(f'    <attribute name="{name}" kind="{kind}" required="{req}">')
            lines += [f"      <option>{escape(o)}</option>" for o in options]
            lines.append("    </attribute>")
        else:
            lines.append(f'    <attribute name="{name}" kind="{kind}" required="{req}"/>')
    lines.append("  </attributes>")
    lines.append("  <outline>")
    lines += [f'    <ref id="{ref}"/>' for ref in outline]
    lines.append("  </outline>")
    lines.append("</document>")
    return "\n".join(lines) + "\n"


def _blind_object(obj_id, attrs, description="text"):
    """An object file. An attribute value of None is written as an empty element <attr/>."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<object id="{obj_id}">']
    for name, value in attrs.items():
        if value is None:
            lines.append(f'  <attr name="{name}"/>')
        else:
            lines.append(f'  <attr name="{name}">{escape(value)}</attr>')
    lines.append(f"  <description>{escape(description)}</description>")
    lines.append("</object>")
    return "\n".join(lines) + "\n"


def _blind_links(src, links):
    """links: (target_doc, target_id, link_type, ack-or-None) tuples."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<links source="{src}">']
    for doc, target, link_type, ack in links:
        ack_part = f' ack="{ack}"' if ack else ""
        lines.append(
            f'  <link target-doc="{doc}" target-id="{target}" type="{link_type}"{ack_part}/>'
        )
    lines.append("</links>")
    return "\n".join(lines) + "\n"


def _blind_rules(rules):
    """rules: (when: dict, attr directives: dict name -> status, link target docs) tuples."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<rules>"]
    for when, attr_directives, link_docs in rules:
        lines.append("  <rule>")
        conds = " ".join(f"{k}={quoteattr(v)}" for k, v in when.items())
        lines.append(f"    <when {conds}/>" if conds else "    <when/>")
        lines.append("    <then>")
        for name, status in attr_directives.items():
            lines.append(f'      <attr name="{name}" status="{status}"/>')
        for doc in link_docs:
            lines.append(f'      <link target-doc="{doc}" status="required"/>')
        lines.append("    </then>")
        lines.append("  </rule>")
    lines.append("</rules>")
    return "\n".join(lines) + "\n"


def _blind_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _blind_write_doc(root, doc_id, objects, *, attrs=_BLIND_DEFAULT_ATTRS, outline=None,
                     parents=(), links=None, rules=None, date="2026-04-21",
                     descriptions=None) -> Path:
    """Write a whole document folder: manifest, objects, links and an optional rules file."""
    folder = Path(root) / doc_id
    (folder / "objects").mkdir(parents=True, exist_ok=True)
    if outline is None:
        outline = list(objects)
    _blind_write(folder / f"{doc_id}.xml",
                 _blind_manifest(doc_id, attrs=attrs, outline=outline, parents=parents,
                                 date=date))
    descriptions = descriptions or {}
    for oid, obj_attrs in objects.items():
        _blind_write(folder / "objects" / f"{oid}.xml",
                     _blind_object(oid, obj_attrs, descriptions.get(oid, f"text of {oid}")))
    for src, src_links in (links or {}).items():
        _blind_write(folder / "links" / f"{src}.xml", _blind_links(src, src_links))
    if rules is not None:
        _blind_write(folder / f"{doc_id}.rules.xml", _blind_rules(rules))
    return folder


def _blind_git_env(root: Path) -> dict[str, str]:
    cfg = Path(root).parent / f".{Path(root).name}-blind.gitconfig"
    if not cfg.exists():
        cfg.write_text(
            "[user]\n\tname = Blind Tester\n\temail = blind@example.invalid\n"
            "[core]\n\tautocrlf = false\n[init]\n\tdefaultBranch = main\n",
            encoding="utf-8",
        )
    env = dict(os.environ)
    env.update(GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0",
               GIT_TERMINAL_PROMPT="0")
    return env


def _blind_git(root: Path, *args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd or root, env=_blind_git_env(root),
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.rstrip()


def _blind_commit(root: Path, message: str) -> str:
    _blind_git(root, "add", "-A")
    _blind_git(root, "commit", "-q", "-m", message)
    return _blind_git(root, "rev-parse", "HEAD")


def _blind_snapshot(root: Path, *, include_git: bool) -> dict[str, tuple[str, int]]:
    """Each file's sha256 and modified time (ns), keyed by its path relative to `root`."""
    out: dict[str, tuple[str, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if not include_git and Path(dirpath) == Path(root) and ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            p = Path(dirpath, name)
            out[p.relative_to(root).as_posix()] = (
                hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return out


def _blind_assert_unchanged(before, after, what: str) -> None:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    assert not (added or removed or changed), (
        f"{what} changed files: added={added} removed={removed} changed={changed}")


def _blind_exe(name: str) -> str:
    exe = shutil.which(name, path=str(Path(sys.executable).parent))
    assert exe, f"{name} is not installed next to {sys.executable}"
    return exe


def _blind_check_process(argv, *, cwd=None, extra_env=None):
    env = dict(os.environ)
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.update(extra_env or {})
    proc = subprocess.run([_blind_exe("mellon-check"), *argv], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)
    return proc.returncode, proc.stdout, proc.stderr


def _blind_check_main(argv, capsys, monkeypatch, *, cwd=None):
    from mellon.check import main

    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    if cwd is not None:
        monkeypatch.chdir(cwd)
    capsys.readouterr()
    try:
        code = main(list(argv))
    except SystemExit as exc:  # a main that exits instead of returning
        code = exc.code if isinstance(exc.code, int) else 1
    out, err = capsys.readouterr()
    return code, out, err


def _blind_run_check(runner, argv, capsys, monkeypatch, *, cwd=None):
    if runner == "process":
        return _blind_check_process(argv, cwd=cwd)
    return _blind_check_main(argv, capsys, monkeypatch, cwd=cwd)


def _blind_clean_check_repo(root: Path) -> Path:
    """Git repository with PARENT and CHILD; CHILD-1 links to the committed PARENT-1 with the
    right acknowledged commit, so there is no check problem."""
    root.mkdir(parents=True, exist_ok=True)
    _blind_git(root, "init", "-q")
    _blind_write_doc(root, "PARENT", {
        "PARENT-0": {"type": "Heading"},
        "PARENT-1": {"type": "Requirement", "status": "Draft"},
        "PARENT-2": {"type": "Requirement", "status": "Draft"},
    })
    parent_commit = _blind_commit(root, "Add PARENT")
    _blind_write_doc(root, "CHILD", {"CHILD-1": {"type": "Requirement", "status": "Draft"}},
                     parents=("PARENT",),
                     links={"CHILD-1": [("PARENT", "PARENT-1", "satisfies", parent_commit[:7])]})
    _blind_commit(root, "Add CHILD")
    return root


_BLIND_PROBLEM_TOKEN = re.compile(
    r"\b(LOAD_ERROR|SUSPECT\w*|BROKEN\w*|[A-Z]+(?:_[A-Z]+)+)\b")


def _blind_problem_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if _BLIND_PROBLEM_TOKEN.search(line)]


_BLIND_CONFLICTED_MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<document id="MERGED">
<<<<<<< HEAD
  <metadata><title>Ours</title></metadata>
=======
  <metadata><title>Theirs</title></metadata>
>>>>>>> other
  <parents/>
  <attributes/>
  <outline/>
</document>
"""


def _blind_clean_pair(root: Path) -> None:
    _blind_write_doc(root, "PARENT", {
        "PARENT-0": {"type": "Heading"},
        "PARENT-1": {"type": "Requirement", "status": "Draft"},
    })
    _blind_write_doc(root, "CHILD", {"CHILD-0": {"type": "Requirement", "status": "Draft"}},
                     parents=("PARENT",),
                     links={"CHILD-0": [("PARENT", "PARENT-1", "satisfies", None)]})


@pytest.mark.parametrize("runner", ["process", "main"])
def test_check_srs_116_without_repo_root_checks_current_folder(tmp_path, capsys, monkeypatch,
                                                               runner):
    """MELLON-CHECK-SRS-116: "When mellon-check runs without --repo-root and the
    CLAUDE_PROJECT_DIR environment variable is unset, mellon-check shall check the current
    folder." Condition: the current folder is a clean two-document repository (exit 0, 2
    documents), or one with a validation error (exit 1)."""
    clean = _blind_clean_check_repo(tmp_path / "clean")
    code, out, err = _blind_run_check(runner, [], capsys, monkeypatch, cwd=clean)
    assert code == 0, (out, err)
    assert re.search(r"\b2 document", out), out

    bad = _blind_clean_check_repo(tmp_path / "bad")
    _blind_write(bad / "CHILD" / "objects" / "CHILD-1.xml",
                 _blind_object("CHILD-1", {"type": "Requirement", "status": "Bogus"}))
    code, out, err = _blind_run_check(runner, [], capsys, monkeypatch, cwd=bad)
    assert code == 1, (out, err)


@pytest.mark.parametrize("runner", ["process", "main"])
def test_check_srs_116_without_repo_root_from_document_folder_exits_2(tmp_path, capsys,
                                                                       monkeypatch, runner):
    """MELLON-CHECK-SRS-116 with -117: run without --repo-root and CLAUDE_PROJECT_DIR unset
    from a document folder inside a clone, the current folder isn't a clone's root: exit
    status 2."""
    root = _blind_clean_check_repo(tmp_path / "repo")
    code, out, err = _blind_run_check(runner, [], capsys, monkeypatch, cwd=root / "CHILD")
    assert code == 2, (out, err)


@pytest.mark.parametrize("runner", ["process", "main"])
@pytest.mark.parametrize("where", ["no-git", "subfolder", "shallow"])
def test_check_srs_117_not_complete_clone_root_exits_2(tmp_path, capsys, monkeypatch, runner,
                                                       where):
    """MELLON-CHECK-SRS-117: "If the folder mellon-check is to check isn't the root folder of
    a complete git clone, mellon-check shall exit with status 2." Condition: a folder with
    documents and no git, a document folder inside a clone, and a shallow clone."""
    if where == "no-git":
        target = tmp_path / "plain"
        _blind_clean_pair(target)
    else:
        root = _blind_clean_check_repo(tmp_path / "repo")
        if where == "subfolder":
            target = root / "CHILD"
        else:
            target = tmp_path / "shallow"
            _blind_git(root, "clone", "-q", "--depth", "1", root.as_uri(), str(target),
                       cwd=tmp_path)
            assert (target / ".git" / "shallow").exists(), "test setup: clone isn't shallow"
    code, out, err = _blind_run_check(runner, ["--repo-root", str(target)], capsys,
                                      monkeypatch)
    assert code == 2, (out, err)


def _blind_make_suspect(root: Path) -> None:
    _blind_write(root / "PARENT" / "objects" / "PARENT-1.xml",
                 _blind_object("PARENT-1", {"type": "Requirement", "status": "Draft"},
                               "Revised parent text"))
    _blind_commit(root, "Revise PARENT-1")


def _blind_make_validation_error(root: Path) -> None:
    _blind_write(root / "CHILD" / "objects" / "CHILD-1.xml",
                 _blind_object("CHILD-1", {"type": "Requirement", "status": "Bogus"}))
    _blind_commit(root, "Bad status")


def _blind_make_load_error(root: Path) -> None:
    _blind_write(root / "MERGED" / "MERGED.xml", _BLIND_CONFLICTED_MANIFEST)
    _blind_commit(root, "Conflicted document")


@pytest.mark.parametrize("runner", ["process", "main"])
def test_check_srs_146_clean_repository_exits_0(tmp_path, capsys, monkeypatch, runner):
    """MELLON-CHECK-SRS-146: "When the repository holds at least one document and
    mellon-check finds no check problem, mellon-check shall exit with status 0." Condition: a
    clean two-document repository whose one link carries the right acknowledged commit."""
    root = _blind_clean_check_repo(tmp_path / "repo")
    code, out, err = _blind_run_check(runner, ["--repo-root", str(root)], capsys, monkeypatch)
    assert code == 0, (out, err)


def test_check_srs_146_clean_repository_exits_0_without_git_identity(tmp_path):
    """MELLON-CHECK-SRS-146: "... finds no check problem, mellon-check shall exit with status
    0." Condition: the program runs as a process with no git user.name or user.email
    configured (a home folder without git configuration); checking needs no identity."""
    root = _blind_clean_check_repo(tmp_path / "repo")
    home = tmp_path / "empty-home"
    home.mkdir()
    env = {"HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home),
           "APPDATA": str(home), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": str(home / "none.gitconfig")}
    code, out, err = _blind_check_process(["--repo-root", str(root)], extra_env=env)
    assert code == 0, (out, err)


@pytest.mark.parametrize("runner", ["process", "main"])
def test_check_srs_147_load_error_exits_1(tmp_path, capsys, monkeypatch, runner):
    """MELLON-CHECK-SRS-147: "If a document has a load error, mellon-check shall exit with
    status 1." Condition: a third document whose manifest holds merge conflict markers."""
    root = _blind_clean_check_repo(tmp_path / "repo")
    _blind_make_load_error(root)
    code, out, err = _blind_run_check(runner, ["--repo-root", str(root)], capsys, monkeypatch)
    assert code == 1, (out, err)


@pytest.mark.parametrize("runner", ["process", "main"])
def test_check_srs_148_validation_error_exits_1(tmp_path, capsys, monkeypatch, runner):
    """MELLON-CHECK-SRS-148: "If a document has a validation error, mellon-check shall exit
    with status 1." Condition: CHILD-1's status isn't one of the options."""
    root = _blind_clean_check_repo(tmp_path / "repo")
    _blind_make_validation_error(root)
    code, out, err = _blind_run_check(runner, ["--repo-root", str(root)], capsys, monkeypatch)
    assert code == 1, (out, err)


@pytest.mark.parametrize("runner", ["process", "main"])
def test_check_srs_149_suspect_link_exits_1(tmp_path, capsys, monkeypatch, runner):
    """MELLON-CHECK-SRS-149: "If a document has a suspect link, mellon-check shall exit with
    status 1." Condition: PARENT-1 is revised and committed after CHILD-1's link was
    acknowledged."""
    root = _blind_clean_check_repo(tmp_path / "repo")
    _blind_make_suspect(root)
    code, out, err = _blind_run_check(runner, ["--repo-root", str(root)], capsys, monkeypatch)
    assert code == 1, (out, err)


@pytest.mark.parametrize("runner", ["process", "main"])
def test_check_srs_127_mellon_check_writes_nothing(tmp_path, capsys, monkeypatch, runner):
    """MELLON-CHECK-SRS-127: "While mellon-check runs, mellon-check shall not change a file in
    the working copy or in the repository's .git folder." Condition: 60 commits of history,
    an uncommitted change, a staged change and an untracked file."""
    root = _blind_clean_check_repo(tmp_path / "repo")
    for n in range(60):
        _blind_git(root, "commit", "-q", "--allow-empty", "-m", f"history {n}")
    _blind_write(root / "CHILD" / "objects" / "CHILD-1.xml",
                 _blind_object("CHILD-1", {"type": "Requirement", "status": "Draft"},
                               "Uncommitted edit"))
    _blind_write(root / "PARENT" / "objects" / "PARENT-2.xml",
                 _blind_object("PARENT-2", {"type": "Requirement", "status": "Draft"},
                               "Staged edit"))
    _blind_git(root, "add", "PARENT/objects/PARENT-2.xml")
    _blind_write(root / "CHILD" / "untracked.txt", "untracked\n")
    status_before = _blind_git(root, "status", "--porcelain=v1", "--untracked-files=all")
    assert "M  PARENT/objects/PARENT-2.xml" in status_before, status_before
    assert " M CHILD/objects/CHILD-1.xml" in status_before, status_before

    before = _blind_snapshot(root, include_git=True)
    code, out, err = _blind_run_check(runner, ["--repo-root", str(root)], capsys, monkeypatch)
    assert code in (0, 1), (out, err)
    _blind_assert_unchanged(before, _blind_snapshot(root, include_git=True), "mellon-check")
    assert _blind_git(root, "status", "--porcelain=v1", "--untracked-files=all") == \
        status_before


def _blind_four_problem_repo(root: Path, *, with_load_error: bool) -> Path:
    _blind_clean_check_repo(root)
    _blind_write_doc(root, "PARENT", {
        "PARENT-0": {"type": "Heading"},
        "PARENT-1": {"type": "Requirement", "status": "Draft"},
        "PARENT-2": {"type": "Requirement", "status": "Bogus"},
    }, outline=["PARENT-0", "PARENT-1", "PARENT-2", "PARENT-9"],
        descriptions={"PARENT-1": "Revised parent text"})
    _blind_commit(root, "Revise PARENT")
    if with_load_error:
        _blind_make_load_error(root)
    return root


@pytest.mark.parametrize("runner", ["process", "main"])
def test_check_srs_134_one_line_per_check_problem(tmp_path, capsys, monkeypatch, runner):
    """MELLON-CHECK-SRS-134: "mellon-check shall print one line for each check problem
    found." Condition: PARENT has ATTR_BAD_ENUM and OUTLINE_MISSING_OBJECT and CHILD one
    suspect link: three problem lines, one of each."""
    root = _blind_four_problem_repo(tmp_path / "repo", with_load_error=False)
    code, out, err = _blind_run_check(runner, ["--repo-root", str(root)], capsys, monkeypatch)
    lines = _blind_problem_lines(out)
    assert len(lines) == 3, out
    for token in ("ATTR_BAD_ENUM", "OUTLINE_MISSING_OBJECT", "SUSPECT"):
        assert sum(token in line for line in lines) == 1, (token, out)


@pytest.mark.parametrize("runner", ["process", "main"])
def test_check_srs_134_one_line_per_check_problem_with_load_error(tmp_path, capsys,
                                                                  monkeypatch, runner):
    """MELLON-CHECK-SRS-134: "mellon-check shall print one line for each check problem
    found." Condition: as above plus a third document whose manifest holds conflict markers:
    four problem lines, one of each."""
    root = _blind_four_problem_repo(tmp_path / "repo", with_load_error=True)
    code, out, err = _blind_run_check(runner, ["--repo-root", str(root)], capsys, monkeypatch)
    lines = _blind_problem_lines(out)
    assert len(lines) == 4, out
    for token in ("LOAD_ERROR", "ATTR_BAD_ENUM", "OUTLINE_MISSING_OBJECT", "SUSPECT"):
        assert sum(token in line for line in lines) == 1, (token, out)


@pytest.mark.parametrize("runner", ["process", "main"])
@pytest.mark.parametrize("target", [
    pytest.param(("CHILD", "CHILD-9"), id="same-document"),
    pytest.param(("PARENT", "PARENT-9"), id="parent-document"),
])
def test_check_srs_134_broken_link_has_one_line(tmp_path, capsys, monkeypatch, runner,
                                                target):
    """MELLON-CHECK-SRS-134: "mellon-check shall print one line for each check problem
    found." (rationale: "A broken link is a validation error, so it has one line"; a check
    problem is a load error, a validation error or a suspect link). Condition: a clean
    repository plus CHILD-1 linking to an object that doesn't exist, in its own document or
    in its parent document: one problem line."""
    root = _blind_clean_check_repo(tmp_path / "repo")
    ack = _blind_git(root, "log", "-1", "--format=%H", "--", "PARENT/objects/PARENT-1.xml")
    _blind_write(root / "CHILD" / "links" / "CHILD-1.xml", _blind_links("CHILD-1", [
        ("PARENT", "PARENT-1", "satisfies", ack[:7]), (*target, "refines", None)]))
    _blind_commit(root, "Link to a missing object")
    code, out, err = _blind_run_check(runner, ["--repo-root", str(root)], capsys, monkeypatch)
    assert code == 1, (out, err)
    assert len(_blind_problem_lines(out)) == 1, out


@pytest.mark.parametrize("runner", ["process", "main"])
def test_check_srs_137_load_error_line_has_document_file_line_and_reason(tmp_path, capsys,
                                                                         monkeypatch, runner):
    """MELLON-CHECK-SRS-137: "mellon-check shall print for each load error a line holding
    LOAD_ERROR, the document ID, the file, the line and the reason." Condition:
    CHILD/objects/CHILD-1.xml holds merge conflict markers from line 3."""
    root = _blind_clean_check_repo(tmp_path / "repo")
    _blind_write(root / "CHILD" / "objects" / "CHILD-1.xml", (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<object id="CHILD-1">\n'
        "<<<<<<< HEAD\n"
        '  <attr name="status">Draft</attr>\n'
        "=======\n"
        '  <attr name="status">Approved</attr>\n'
        ">>>>>>> other\n"
        '  <attr name="type">Requirement</attr>\n'
        "  <description>text</description>\n"
        "</object>\n"))
    code, out, err = _blind_run_check(runner, ["--repo-root", str(root)], capsys, monkeypatch)
    lines = [line for line in out.splitlines() if "LOAD_ERROR" in line]
    assert len(lines) == 1, out
    line = lines[0]
    assert "CHILD" in line, line
    assert "CHILD/objects/CHILD-1.xml" in line.replace("\\", "/"), line
    assert re.search(r"(line\s*3\b|:3\b)", line), f"line 3 isn't named: {line}"
    assert "merge conflict" in line.lower(), line


# MELLON-STORE-SRS blind tests
from tests.store_support import (  # noqa: E402
    CHECK_EXE,
    _norm,
    changed,
    git,
    not_in_work_tree,
    run_program,
    snapshot,
)


@pytest.mark.parametrize("program", [CHECK_EXE], ids=["check"])
def test_store_srs_7_subfolder_refusal_names_the_root(clone_with_doc, program):
    """MELLON-STORE-SRS-7: when Mellon refuses a subfolder of a git work tree,
    Mellon shall name the work tree's root folder. Started on a document folder
    inside a clone, the program exits 2 and its error output names the root
    (not only the subfolder, whose path contains the root's)."""
    root = clone_with_doc
    sub = root / "DOC"
    code, out = run_program(program, sub)
    assert code == 2, f"expected exit 2 for a subfolder, got {code}: {out}"
    rest = _norm(out).replace(_norm(str(sub)), "<subfolder>")
    assert _norm(str(root)) in rest, f"error output doesn't name the root {root}: {out!r}"


def test_store_srs_9_check_refuses_repo_git_cant_open(unopenable_repo):
    """MELLON-STORE-SRS-9: if git can't open the repository, Mellon shall refuse
    the folder: mellon-check exits 2 on a repository with format version 99."""
    code, out = run_program(CHECK_EXE, unopenable_repo)
    assert code == 2, f"expected exit 2, got {code}: {out}"


@pytest.mark.parametrize("program", [CHECK_EXE], ids=["check"])
def test_store_srs_12_each_refusal_gives_its_own_reason(
    clone_with_doc, unopenable_repo, tmp_path, git_env, program
):
    """MELLON-STORE-SRS-12: when Mellon refuses the folder, Mellon shall report
    whether it is outside a git work tree, a subfolder of one, a shallow clone,
    or a repository git can't open. Each case exits 2 with a reason that names
    its own case, and the four reasons differ once paths are taken out."""
    outside = tmp_path / "outside"
    outside.mkdir()
    if not not_in_work_tree(git_env, outside):
        pytest.skip(f"{outside} is inside a git work tree on this machine")
    shallow = tmp_path / "shallow"
    git(git_env, tmp_path, "clone", "-q", "--depth", "1", clone_with_doc.as_uri(), str(shallow))
    cases = {
        "outside": (outside, None),
        "subfolder": (clone_with_doc / "DOC", clone_with_doc),
        "shallow": (shallow, None),
        "cant-open": (unopenable_repo, None),
    }
    reasons = {}
    for name, (folder, root) in cases.items():
        code, out = run_program(program, folder)
        assert code == 2, f"{name}: expected exit 2, got {code}: {out}"
        text = _norm(out).replace(_norm(str(folder)), "<folder>")
        if root is not None:
            text = text.replace(_norm(str(root)), "<root>")
        reasons[name] = text.strip()
    assert "git" in reasons["outside"], reasons["outside"]
    assert "<root>" in reasons["subfolder"], reasons["subfolder"]
    assert "shallow" in reasons["shallow"], reasons["shallow"]
    assert "open" in reasons["cant-open"], reasons["cant-open"]
    assert len(set(reasons.values())) == 4, f"reasons don't differ: {reasons}"


def test_store_srs_14_check_accepts_root_without_commit(uncommitted_repo):
    """MELLON-STORE-SRS-14: Mellon shall accept a repository root whose repository
    has no commit: mellon-check doesn't exit 2 (the root can't be checked)."""
    code, out = run_program(CHECK_EXE, uncommitted_repo)
    assert code != 2, f"mellon-check refused the root: {out}"


def test_store_srs_15_check_accepts_detached_head(detached_repo):
    """MELLON-STORE-SRS-15: Mellon shall accept a repository root whose HEAD is
    detached: mellon-check checks it (exit 0 for this clean document)."""
    code, out = run_program(CHECK_EXE, detached_repo)
    assert code == 0, f"exit {code}: {out}"


def test_store_srs_210_check_changes_no_file(read_only_repo):
    """MELLON-STORE-SRS-210: when Mellon checks a repository (mellon-check), it
    leaves each data file and figure file unchanged."""
    root, _ = read_only_repo
    before = snapshot(root, include_git=False)
    run_program(CHECK_EXE, root)
    assert changed(before, snapshot(root, include_git=False)) == []

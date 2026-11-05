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

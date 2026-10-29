"""Shared fixtures: build a minimal valid document tree on disk."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pygit2
import pytest


def _ensure_req_test_fixture() -> None:
    """Clone `req-test.bundle` into `<project-root>/req-test/` if missing.

    The fixture repo isn't checked in directly (nested-repo conflict with the
    outer code repo); instead the top-level `req-test.bundle` file carries its
    full history. Silent no-op when `req-test/` already exists; silent skip
    when the bundle isn't present either (tests that need the fixture already
    guard with `pytest.skip`).
    """
    project_root = Path(__file__).resolve().parents[2]
    target = project_root / "req-test"
    bundle = project_root / "req-test.bundle"
    if target.exists() or not bundle.exists():
        return
    try:
        subprocess.run(
            ["git", "clone", "--quiet", str(bundle), str(target)],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        # `git` missing or clone failed — skip silently. Dependent tests will
        # notice `req-test/.git` is absent and skip themselves.
        pass


_ensure_req_test_fixture()

# CI sets this so a missing fixture fails loudly instead of silently skipping
# the ~390 GUI/MCP tests that depend on it.
if os.environ.get("MELLON_REQUIRE_FIXTURE") and not (
    Path(__file__).resolve().parents[2] / "req-test" / ".git"
).exists():
    raise pytest.UsageError(
        "MELLON_REQUIRE_FIXTURE is set but req-test/ could not be cloned "
        "from req-test.bundle (is git on PATH?)"
    )


def _clean_worktree(root: Path) -> None:
    """Reset a copied git working tree to its HEAD commit.

    Drops uncommitted changes to tracked files and removes untracked files.
    Required because the source `req-test/` occasionally carries work-in-
    progress state that would otherwise leak into tests.
    """
    if not (root / ".git").exists():
        return
    repo = pygit2.Repository(str(root))
    try:
        head = repo.head.target
    except (pygit2.GitError, KeyError):
        return
    repo.reset(head, pygit2.GIT_RESET_HARD)
    for path, flags in repo.status().items():
        if flags & pygit2.GIT_STATUS_WT_NEW:
            full = root / path
            if full.is_file():
                full.unlink()


def _fixture_commit(repo: pygit2.Repository, summary: str) -> pygit2.Commit:
    """The commit reachable from HEAD whose message starts with `summary`.

    Tests name fixture commits by message rather than hash, so the fixture's
    history can be rewritten (which changes every hash) without touching them.
    """
    for commit in repo.walk(repo.head.target):
        if commit.message.startswith(summary):
            return commit
    raise LookupError(f"no fixture commit starting with {summary!r}")


@pytest.fixture(autouse=True, scope="session")
def _git_identity(tmp_path_factory):
    """Point libgit2's global config at a test-only one holding a git identity.

    Commits refuse to run without user.name/user.email, and tests that commit
    in temp repos must not depend on the developer's (or CI's) own identity.
    """
    home = tmp_path_factory.mktemp("git-global")
    (home / ".gitconfig").write_text(
        "[user]\n\tname = Mellon Test\n\temail = test@mellon.invalid\n",
        encoding="utf-8",
    )
    levels = (pygit2.GIT_CONFIG_LEVEL_GLOBAL, pygit2.GIT_CONFIG_LEVEL_XDG)
    saved = {level: pygit2.settings.search_path[level] for level in levels}
    for level in levels:
        pygit2.settings.search_path[level] = str(home)
    yield
    for level, path in saved.items():
        pygit2.settings.search_path[level] = path


@pytest.fixture
def clean_worktree():
    """Fixture wrapper so test modules can inject the helper without importing
    conftest (which pytest doesn't expose as a regular module)."""
    return _clean_worktree


@pytest.fixture
def fixture_commit():
    """Fixture wrapper for `_fixture_commit`, like `clean_worktree`."""
    return _fixture_commit


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path_factory, monkeypatch, request):
    """Every test gets its own `(org, app)` pair + tmp INI path.

    Using a unique org per test defeats Qt's internal settings cache
    (which keys by org/app/format) — setPath alone isn't enough because
    subsequent constructs of `QSettings("mellon","mellon")` may still
    be served from a cached backing store created by the previous test.
    """
    try:
        from PySide6.QtCore import QSettings
        from mellon.gui import settings as gui_settings
    except ImportError:
        return
    settings_root = tmp_path_factory.mktemp("qsettings")
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(settings_root),
    )
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.SystemScope,
        str(settings_root / "system"),
    )
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    # Unique org per test so no state can carry over in Qt's settings cache.
    unique_org = f"mellon-test-{abs(hash(request.node.nodeid)) & 0xFFFFFFFF:x}"
    monkeypatch.setattr(gui_settings, "ORG", unique_org)
    monkeypatch.setattr(gui_settings, "APP", "mellon")


@pytest.fixture(autouse=True)
def _reset_theme_manager():
    """Reset the ThemeManager singleton between tests so a theme change in
    one test doesn't bleed into the next."""
    try:
        from mellon.gui.theme import ThemeManager
    except ImportError:
        return
    ThemeManager.reset_for_tests()
    yield
    ThemeManager.reset_for_tests()


MANIFEST = """\
<?xml version="1.0" encoding="UTF-8"?>
<document id="DOC">
  <metadata>
    <title>Test doc</title>
    <author>tester</author>
    <date>2026-04-21</date>
  </metadata>
  <parents><parent doc="PARENT"/></parents>
  <attributes>
    <attribute name="type" kind="enum" required="true">
      <option>Heading</option>
      <option>Information</option>
      <option>Note</option>
      <option>Requirement</option>
    </attribute>
    <attribute name="status" kind="enum" required="true">
      <option>Draft</option>
      <option>Approved</option>
    </attribute>
    <attribute name="rationale" kind="text" required="false"/>
  </attributes>
  <outline>
    <ref id="DOC-0">
      <ref id="DOC-1"/>
    </ref>
  </outline>
</document>
"""

OBJ_0 = """\
<?xml version="1.0" encoding="UTF-8"?>
<object id="DOC-0">
  <attr name="type">Heading</attr>
  <attr name="status">Draft</attr>
  <description>Top heading</description>
</object>
"""

OBJ_1 = """\
<?xml version="1.0" encoding="UTF-8"?>
<object id="DOC-1">
  <attr name="type">Requirement</attr>
  <attr name="status">Draft</attr>
  <attr name="rationale">because</attr>
  <description>The system shall do X.</description>
</object>
"""

LINKS_1 = """\
<?xml version="1.0" encoding="UTF-8"?>
<links source="DOC-1">
  <link target-doc="PARENT" target-id="PARENT-42" type="satisfies" ack="abc1234"/>
</links>
"""


@pytest.fixture
def doc_folder(tmp_path: Path) -> Path:
    """A valid on-disk document rooted at tmp_path/DOC."""
    root = tmp_path / "DOC"
    (root / "objects").mkdir(parents=True)
    (root / "links").mkdir()
    (root / "DOC.xml").write_text(MANIFEST, encoding="utf-8")
    (root / "objects" / "DOC-0.xml").write_text(OBJ_0, encoding="utf-8")
    (root / "objects" / "DOC-1.xml").write_text(OBJ_1, encoding="utf-8")
    (root / "links" / "DOC-1.xml").write_text(LINKS_1, encoding="utf-8")
    return root


# --- MCP fixtures ----------------------------------------------------------

_MCP_PARENT_MANIFEST = """\
<?xml version="1.0" encoding="UTF-8"?>
<document id="PARENT">
  <metadata>
    <title>Parent doc</title>
    <author>tester</author>
    <date>2026-04-22</date>
  </metadata>
  <attributes>
    <attribute name="type" kind="enum" required="true">
      <option>Heading</option>
      <option>Information</option>
      <option>Note</option>
      <option>Requirement</option>
    </attribute>
    <attribute name="status" kind="enum" required="true">
      <option>Draft</option>
      <option>Approved</option>
    </attribute>
  </attributes>
  <outline>
    <ref id="PARENT-0">
      <ref id="PARENT-1"/>
    </ref>
  </outline>
</document>
"""

_MCP_PARENT_OBJ_0 = """\
<?xml version="1.0" encoding="UTF-8"?>
<object id="PARENT-0">
  <attr name="type">Heading</attr>
  <attr name="status">Approved</attr>
  <description>Parent top heading</description>
</object>
"""

_MCP_PARENT_OBJ_1 = """\
<?xml version="1.0" encoding="UTF-8"?>
<object id="PARENT-1">
  <attr name="type">Requirement</attr>
  <attr name="status">Approved</attr>
  <description>The system shall parent.</description>
</object>
"""

_MCP_CHILD_MANIFEST = """\
<?xml version="1.0" encoding="UTF-8"?>
<document id="CHILD">
  <metadata>
    <title>Child doc</title>
    <author>tester</author>
    <date>2026-04-22</date>
  </metadata>
  <parents><parent doc="PARENT"/></parents>
  <attributes>
    <attribute name="type" kind="enum" required="true">
      <option>Heading</option>
      <option>Information</option>
      <option>Note</option>
      <option>Requirement</option>
    </attribute>
    <attribute name="status" kind="enum" required="true">
      <option>Draft</option>
      <option>Approved</option>
    </attribute>
  </attributes>
  <outline>
    <ref id="CHILD-0"/>
  </outline>
</document>
"""

_MCP_CHILD_OBJ_0 = """\
<?xml version="1.0" encoding="UTF-8"?>
<object id="CHILD-0">
  <attr name="type">Requirement</attr>
  <attr name="status">Draft</attr>
  <description>The system shall child.</description>
</object>
"""

_MCP_CHILD_LINKS_0 = """\
<?xml version="1.0" encoding="UTF-8"?>
<links source="CHILD-0">
  <link target-doc="PARENT" target-id="PARENT-1" type="satisfies"/>
</links>
"""


@pytest.fixture
def mcp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two-doc repo (PARENT + CHILD) with one un-acked link. Not a git repo.

    Sets `mellon.mcp.server._State.repo_root` for the test and restores on
    teardown via monkeypatch.
    """
    from mellon.mcp import server as _server

    (tmp_path / "PARENT" / "objects").mkdir(parents=True)
    (tmp_path / "PARENT" / "PARENT.xml").write_text(
        _MCP_PARENT_MANIFEST, encoding="utf-8"
    )
    (tmp_path / "PARENT" / "objects" / "PARENT-0.xml").write_text(
        _MCP_PARENT_OBJ_0, encoding="utf-8"
    )
    (tmp_path / "PARENT" / "objects" / "PARENT-1.xml").write_text(
        _MCP_PARENT_OBJ_1, encoding="utf-8"
    )
    (tmp_path / "CHILD" / "objects").mkdir(parents=True)
    (tmp_path / "CHILD" / "links").mkdir()
    (tmp_path / "CHILD" / "CHILD.xml").write_text(
        _MCP_CHILD_MANIFEST, encoding="utf-8"
    )
    (tmp_path / "CHILD" / "objects" / "CHILD-0.xml").write_text(
        _MCP_CHILD_OBJ_0, encoding="utf-8"
    )
    (tmp_path / "CHILD" / "links" / "CHILD-0.xml").write_text(
        _MCP_CHILD_LINKS_0, encoding="utf-8"
    )
    monkeypatch.setattr(_server._State, "repo_root", tmp_path.resolve())
    monkeypatch.setattr(_server._State, "read_only", False)
    return tmp_path

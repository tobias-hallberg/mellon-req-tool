"""Tests for src/mellon/programs.py: finding the programs Mellon runs."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from mellon import programs


def _program(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    prog = folder / (f"{name}.exe" if sys.platform == "win32" else name)
    prog.write_bytes(b"")
    prog.chmod(0o755)
    return prog


@pytest.fixture
def start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Mellon started in tmp_path/start, with Windows' current-folder search on."""
    folder = tmp_path / "start"
    folder.mkdir()
    monkeypatch.chdir(folder)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setenv("PATH", "")
    return folder


# MELLON-PDF-SRS-67 and MELLON-EDIT-SRS-247 name the places left out.


def test_pdf_srs_67_which_never_looks_in_the_start_folder(start: Path) -> None:
    _program(start, "tool")
    assert programs.which("tool") is None


def test_pdf_srs_67_which_skips_relative_path_entries(
    start: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _program(start / "bin", "tool")
    monkeypatch.setenv("PATH", os.pathsep.join(["bin", ".", ""]))
    assert programs.which("tool") is None


def test_pdf_srs_67_which_skips_path_entries_inside_the_repository(
    start: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _program(repo / "deep" / "bin", "tool")
    monkeypatch.setenv("PATH", str(repo / "deep" / "bin"))
    assert programs.which("tool", repo) is None
    # Without a repository to leave out, the same entry is an ordinary folder.
    assert programs.which("tool") is not None


def test_pdf_srs_67_which_returns_the_first_allowed_match(
    start: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _program(repo / "bin", "tool")
    first = _program(tmp_path / "a", "tool")
    _program(tmp_path / "b", "tool")
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([str(repo / "bin"), f'"{tmp_path / "a"}"', str(tmp_path / "b")]),
    )
    assert Path(programs.which("tool", repo)) == first


def test_pdf_srs_67_allowed_leaves_out_the_start_folder_but_not_below_it(
    start: Path, tmp_path: Path
) -> None:
    assert not programs.allowed(start / "tool.exe")
    assert programs.allowed(start / "tools" / "tool.exe")
    assert not programs.allowed(tmp_path / "repo" / "x" / "tool.exe", tmp_path / "repo")
    assert programs.allowed(tmp_path / "other" / "tool.exe", tmp_path / "repo")


@pytest.mark.skipif(sys.platform != "win32", reason="PATHEXT is Windows'")
def test_which_tries_each_pathext_suffix(
    start: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "tool.cmd").write_bytes(b"")
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    assert Path(programs.which("tool")) == folder / "tool.cmd"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no execute bit")
def test_which_needs_the_execute_bit(
    start: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prog = _program(tmp_path / "bin", "tool")
    prog.chmod(0o644)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert programs.which("tool") is None

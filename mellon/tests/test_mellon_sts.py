"""MELLON-STS lists tests that exist.

Not the test of a source module: it checks this repo's own specification
(CLAUDE.md, "Mellon specifies Mellon"). A MELLON-STS test case lists, under
Automated Tests, the pytest node ids that implement it, relative to
`mellon/`. Each must name a test that pytest would collect, so renaming or
deleting a test can't leave its test case pointing at nothing.

The test files are read, not imported: a node id is checked against the
file's syntax tree (module-level `test*` functions, and `test*` methods of
`Test*` classes). For a parametrized id, only the function is checked, not
the parameter set.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

from mellon import load_document

MELLON = Path(__file__).resolve().parents[1]
STS = MELLON.parent / "MELLON-STS"
ATTR = "automated-tests"


def _listed() -> list[tuple[str, str]]:
    """`(test case id, node id)` for every node id MELLON-STS lists."""
    doc = load_document(STS)
    return [
        (obj_id, node_id)
        for obj_id, obj in doc.objects.items()
        for node_id in obj.attrs.get(ATTR, "").split()
    ]


@cache
def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _defs(body: list[ast.stmt]) -> dict[str, ast.stmt]:
    return {
        node.name: node
        for node in body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def node_id_problem(node_id: str, root: Path = MELLON) -> str | None:
    """Why `node_id` names no test under `root`, or None when it does."""
    file, sep, rest = node_id.partition("::")
    if not sep or not rest:
        return "not a node id (expected tests/<file>.py::<test>)"
    path = root / file
    if not (file.startswith("tests/") and Path(file).name.startswith("test_") and file.endswith(".py")):
        return f"{file} isn't a test file under tests/"
    if not path.is_file():
        return f"{file} doesn't exist"
    *classes, name = rest.split("::")
    name = name.split("[", 1)[0]  # a parametrized test: check the function
    scope = _defs(_module(path).body)
    for cls in classes:
        node = scope.get(cls)
        if not isinstance(node, ast.ClassDef) or not cls.startswith("Test"):
            return f"no test class {cls} in {file}"
        scope = _defs(node.body)
    node = scope.get(name)
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not name.startswith("test"):
        return f"no test {name} in {file}"
    return None


def test_mellon_sts_node_ids_name_existing_tests() -> None:
    listed = _listed()
    assert listed, f"MELLON-STS lists no {ATTR}: was the attribute renamed?"
    problems = [
        f"{obj_id}: {node_id}: {problem}"
        for obj_id, node_id in listed
        if (problem := node_id_problem(node_id)) is not None
    ]
    assert not problems, (
        "MELLON-STS test cases list tests that don't exist; update their "
        f"Automated Tests:\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize(
    ("node_id", "problem"),
    [
        ("tests/test_x.py::test_a", None),
        ("tests/test_x.py::test_p[1-2]", None),
        ("tests/test_x.py::TestGroup::test_m", None),
        ("tests/test_x.py::test_gone", "no test test_gone in tests/test_x.py"),
        ("tests/test_x.py::helper", "no test helper in tests/test_x.py"),
        ("tests/test_x.py::TestGroup::test_gone", "no test test_gone in tests/test_x.py"),
        ("tests/test_x.py::Other::test_m", "no test class Other in tests/test_x.py"),
        ("tests/test_y.py::test_a", "tests/test_y.py doesn't exist"),
        ("src/mellon/diff.py::compare_document", "src/mellon/diff.py isn't a test file under tests/"),
        ("tests/test_x.py", "not a node id (expected tests/<file>.py::<test>)"),
    ],
)
def test_node_id_problem(tmp_path: Path, node_id: str, problem: str | None) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_a():\n    pass\n\n"
        "def test_p(x):\n    pass\n\n"
        "def helper():\n    pass\n\n"
        "class TestGroup:\n    def test_m(self):\n        pass\n\n"
        "class Other:\n    def test_m(self):\n        pass\n",
        encoding="utf-8",
    )
    assert node_id_problem(node_id, tmp_path) == problem

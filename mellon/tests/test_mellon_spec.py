"""Mellon's own specification holds together.

Not the test of a source module: it checks this repo's own requirements and
test documents (`docs/specification.md`, CLAUDE.md "Mellon specifies
Mellon"). The requirements are in the `MELLON-<AREA>-SRS` documents and the
test cases in `MELLON-<AREA>-STS`.

- A test case lists, under Automated Tests, the pytest node ids that
  implement it, relative to `mellon/`. Each must name a test that pytest
  would collect, so renaming or deleting a test can't leave its test case
  pointing at nothing. The test files are read, not imported: a node id is
  checked against the file's syntax tree (module-level `test*` functions,
  and `test*` methods of `Test*` classes). For a parametrized id, only the
  function is checked, not the parameter set.
- Each requirement says exactly one of shall, should or could.
- Each Accepted or Implemented requirement is verified by a test case that
  isn't Withdrawn or Deferred. Mellon's coverage report can't tell this,
  because it counts every non-heading object, withdrawn ones included.

The wording, level and tracing scripts of the engineering framework check
more, but they live outside this repository; see `docs/specification.md`.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from functools import cache
from pathlib import Path

import pytest

from mellon import Document, load_document
from mellon.schema import Link, Metadata, OutlineNode, RequirementObject
from mellon.validator import validate

MELLON = Path(__file__).resolve().parents[1]
REPO = MELLON.parent
ATTR = "automated-tests"
VERIFIED = {"Accepted", "Implemented"}
NOT_VERIFYING = {"Withdrawn", "Deferred"}
KEYWORD = re.compile(r"\b(shall|should|could)\b", re.IGNORECASE)


def _docs(suffix: str) -> list[Document]:
    """The spec documents whose ID starts with `MELLON-` and ends in `suffix`."""
    return [
        load_document(folder)
        for folder in sorted(REPO.iterdir())
        if folder.name.startswith("MELLON-")
        and folder.name.endswith(suffix)
        and (folder / f"{folder.name}.xml").is_file()
    ]


@cache
def _requirement_docs() -> tuple[Document, ...]:
    return tuple(_docs("-SRS"))


@cache
def _test_docs() -> tuple[Document, ...]:
    return tuple(_docs("-STS"))


def _split_area(doc: Document) -> bool:
    """True for a `MELLON-<AREA>-SRS/STS` document, False for the pre-split ones."""
    return doc.id.count("-") >= 2


def _listed() -> list[tuple[str, str]]:
    """`(test case id, node id)` for every node id a test document lists."""
    return [
        (obj_id, node_id)
        for doc in _test_docs()
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


def keyword_problem(text: str) -> str | None:
    """Why a requirement's text breaks the one-keyword rule, or None."""
    found = KEYWORD.findall(text)
    if len(found) != 1:
        return f"{len(found)} uses of shall, should or could"
    return None


def unverified(srs: list[Document], sts: list[Document]) -> list[str]:
    """Accepted or Implemented requirements that no live test case verifies."""
    verified = {
        (link.target_doc, link.target_id)
        for doc in sts
        for source_id, links in doc.links.items()
        if doc.objects.get(source_id) is not None
        and doc.objects[source_id].attrs.get("type") == "Test Case"
        and doc.objects[source_id].attrs.get("status") not in NOT_VERIFYING
        for link in links
        if link.type == "verifies"
    }
    return [
        f"{doc.id}: {obj_id} is {obj.attrs['status']} but no test case verifies it"
        for doc in srs
        for obj_id, obj in sorted(doc.objects.items())
        if obj.attrs.get("type") == "Requirement"
        and obj.attrs.get("status") in VERIFIED
        and (doc.id, obj_id) not in verified
    ]


def test_spec_documents_exist() -> None:
    assert _requirement_docs(), "no MELLON-*-SRS document found: was the layout changed?"
    assert _test_docs(), "no MELLON-*-STS document found: was the layout changed?"


def test_mellon_sts_node_ids_name_existing_tests() -> None:
    listed = _listed()
    assert listed, f"no test document lists {ATTR}: was the attribute renamed?"
    problems = [
        f"{obj_id}: {node_id}: {problem}"
        for obj_id, node_id in listed
        if (problem := node_id_problem(node_id)) is not None
    ]
    assert not problems, (
        "test cases list tests that don't exist; update their "
        f"Automated Tests:\n  " + "\n  ".join(problems)
    )


def test_each_requirement_says_one_keyword() -> None:
    problems = [
        f"{obj_id}: {problem}: {obj.description}"
        for doc in _requirement_docs()
        if _split_area(doc)
        for obj_id, obj in sorted(doc.objects.items())
        if obj.attrs.get("type") == "Requirement"
        and (problem := keyword_problem(obj.description)) is not None
    ]
    assert not problems, "requirements need exactly one of shall, should or could:\n  " + "\n  ".join(problems)


def test_accepted_and_implemented_requirements_are_verified() -> None:
    problems = unverified(list(_requirement_docs()), list(_test_docs()))
    assert not problems, "\n  ".join(["requirements without a test case:", *problems])


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


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("The dialog shall swap From and To.", None),
        ("The dialog should swap From and To.", None),
        ("The dialog could copy the list.", None),
        ("The dialog swaps From and To.", "0 uses of shall, should or could"),
        ("The dialog shall swap, and should beep.", "2 uses of shall, should or could"),
        ("Shall the dialog shall?", "2 uses of shall, should or could"),
    ],
)
def test_keyword_problem(text: str, problem: str | None) -> None:
    assert keyword_problem(text) == problem


def _obj(obj_id: str, **attrs: str) -> RequirementObject:
    return RequirementObject(id=obj_id, attrs=attrs)


def _doc(doc_id: str, objects: list, links: dict | None = None) -> Document:
    return Document(
        id=doc_id,
        metadata=Metadata(),
        parents=(),
        attributes=(),
        outline=(),
        objects={o.id: o for o in objects},
        links=links or {},
    )


def _rule_codes(doc: Document, obj, links: tuple = ()) -> set[str]:
    """The rule violations validation reports for `obj` added to `doc`."""
    broken = dataclasses.replace(
        doc,
        objects={**doc.objects, obj.id: obj},
        links={**doc.links, obj.id: links} if links else doc.links,
        outline=(*doc.outline, OutlineNode(obj.id)),
    )
    return {e.code for e in validate(broken) if obj.id in e.message and e.code.startswith("RULE_")}


@pytest.mark.parametrize("doc", _requirement_docs(), ids=lambda d: d.id)
def test_requirement_rules_catch_missing_attributes(doc: Document) -> None:
    """Each requirements document's rules fail a bare requirement: they are checkers too."""
    if not _split_area(doc):
        pytest.skip("the pre-split document has no rules file")
    assert _rule_codes(doc, _obj(f"{doc.id}-999999", type="Requirement")) == {"RULE_REQUIRED_MISSING"}


@pytest.mark.parametrize("doc", _test_docs(), ids=lambda d: d.id)
def test_test_case_rules_catch_missing_attributes_and_links(doc: Document) -> None:
    if not _split_area(doc):
        pytest.skip("the pre-split document has no rules file")
    bare = _obj(f"{doc.id}-999999", type="Test Case", execution="Automated")
    assert _rule_codes(doc, bare) == {"RULE_REQUIRED_MISSING", "RULE_LINK_REQUIRED_MISSING"}


def test_unverified_catches_a_requirement_without_a_live_test_case() -> None:
    """The coverage check fails when it should: it's a checker too."""
    srs = _doc("A-SRS", [
        _obj("A-SRS-1", type="Requirement", status="Implemented"),
        _obj("A-SRS-2", type="Requirement", status="Accepted"),
        _obj("A-SRS-3", type="Requirement", status="Proposed"),
        _obj("A-SRS-4", type="Requirement", status="Implemented"),
        _obj("A-SRS-5", type="Heading"),
    ])
    sts = _doc(
        "A-STS",
        [
            _obj("A-STS-1", type="Test Case", status="Implemented"),
            _obj("A-STS-2", type="Test Case", status="Withdrawn"),
            _obj("A-STS-3", type="Information"),
        ],
        {
            "A-STS-1": (Link("A-SRS", "A-SRS-1", "verifies", None),),
            "A-STS-2": (Link("A-SRS", "A-SRS-2", "verifies", None),),
            "A-STS-3": (Link("A-SRS", "A-SRS-4", "verifies", None),),
        },
    )
    assert unverified([srs], [sts]) == [
        "A-SRS: A-SRS-2 is Accepted but no test case verifies it",
        "A-SRS: A-SRS-4 is Implemented but no test case verifies it",
    ]

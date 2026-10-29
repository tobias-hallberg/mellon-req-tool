import lxml.etree as ET
import pygit2

import mellon


def test_imports() -> None:
    assert mellon is not None


def test_lxml_parses() -> None:
    tree = ET.fromstring(b"<a><b/></a>")
    assert tree.tag == "a"
    assert tree[0].tag == "b"


def test_pygit2_has_version() -> None:
    assert pygit2.__version__

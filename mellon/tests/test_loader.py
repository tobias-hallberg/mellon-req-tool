from __future__ import annotations

from pathlib import Path

import pytest

from mellon import load_document
from mellon.loader import LoaderError


def test_load_minimal_document(doc_folder: Path) -> None:
    doc = load_document(doc_folder)
    assert doc.id == "DOC"
    assert doc.metadata.title == "Test doc"
    assert doc.parents == ("PARENT",)

    assert {a.name for a in doc.attributes} == {"type", "status", "rationale"}
    type_attr = doc.attribute("type")
    assert type_attr is not None
    assert type_attr.kind == "enum"
    assert type_attr.required is True
    assert "Requirement" in type_attr.options

    assert set(doc.objects) == {"DOC-0", "DOC-1"}
    assert doc.objects["DOC-1"].attrs["rationale"] == "because"

    assert doc.iter_outline_ids() == ["DOC-0", "DOC-1"]

    assert list(doc.links) == ["DOC-1"]
    (link,) = doc.links["DOC-1"]
    assert link.target_doc == "PARENT"
    assert link.target_id == "PARENT-42"
    assert link.type == "satisfies"
    assert link.ack == "abc1234"


def test_missing_manifest(tmp_path: Path) -> None:
    (tmp_path / "DOC").mkdir()
    with pytest.raises(LoaderError, match="Manifest not found"):
        load_document(tmp_path / "DOC")


def test_folder_name_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "WRONG"
    root.mkdir()
    (root / "WRONG.xml").write_text(
        '<?xml version="1.0"?><document id="DIFFERENT"/>', encoding="utf-8"
    )
    with pytest.raises(LoaderError, match="does not match folder name"):
        load_document(root)


def test_object_filename_must_match_id(doc_folder: Path) -> None:
    bad = doc_folder / "objects" / "DOC-1.xml"
    bad.write_text(
        '<?xml version="1.0"?>\n<object id="DOC-99"><description>x</description></object>',
        encoding="utf-8",
    )
    with pytest.raises(LoaderError, match="filename stem"):
        load_document(doc_folder)


def test_unreadable_file_is_loader_error(doc_folder: Path) -> None:
    # lxml raises OSError for a file it can't read (locked, no permission,
    # or, portably, a directory named *.xml); that must not escape raw.
    (doc_folder / "objects" / "DOC-9.xml").mkdir()
    with pytest.raises(LoaderError, match="cannot read file"):
        load_document(doc_folder)


def test_link_without_ack_is_accepted(doc_folder: Path) -> None:
    (doc_folder / "links" / "DOC-1.xml").write_text(
        """<?xml version="1.0"?>
<links source="DOC-1">
  <link target-doc="PARENT" target-id="PARENT-42" type="satisfies"/>
</links>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    (link,) = doc.links["DOC-1"]
    assert link.ack is None


def test_load_document_without_rules_file(doc_folder: Path) -> None:
    doc = load_document(doc_folder)
    assert doc.rules == ()


def test_load_document_with_rules_file(doc_folder: Path) -> None:
    (doc_folder / "DOC.rules.xml").write_text(
        """<?xml version="1.0"?>
<rules>
  <rule>
    <when type="Heading"/>
    <then>
      <attr name="rationale" status="forbidden"/>
    </then>
  </rule>
  <rule>
    <when type="Functional" has-outgoing-link="false"/>
    <then>
      <attr name="rationale" status="required"/>
    </then>
  </rule>
</rules>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert len(doc.rules) == 2
    assert doc.rules[0].conditions == {"type": "Heading"}
    assert doc.rules[0].directives == {"rationale": "forbidden"}
    assert doc.rules[1].conditions == {"type": "Functional", "has-outgoing-link": "false"}
    assert doc.rules[1].directives == {"rationale": "required"}


def test_load_rules_file_accepts_attr_equality_predicate(doc_folder: Path) -> None:
    # Free-form predicate: any unreserved <when @key=val> matches obj.attrs[key].
    (doc_folder / "DOC.rules.xml").write_text(
        """<?xml version="1.0"?>
<rules>
  <rule>
    <when status="Approved"/>
    <then><attr name="rationale" status="required"/></then>
  </rule>
</rules>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert len(doc.rules) == 1
    assert doc.rules[0].conditions == {"status": "Approved"}


def test_load_rules_file_parses_link_directive(doc_folder: Path) -> None:
    (doc_folder / "DOC.rules.xml").write_text(
        """<?xml version="1.0"?>
<rules>
  <rule>
    <when type="Functional"/>
    <then><link target-doc="PARENT" status="required"/></then>
  </rule>
</rules>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert len(doc.rules[0].link_directives) == 1
    ld = doc.rules[0].link_directives[0]
    assert ld.target_doc == "PARENT"
    assert ld.status == "required"


def test_load_rules_file_rejects_link_without_target(doc_folder: Path) -> None:
    (doc_folder / "DOC.rules.xml").write_text(
        """<?xml version="1.0"?>
<rules>
  <rule><when type="X"/><then><link status="required"/></then></rule>
</rules>
""",
        encoding="utf-8",
    )
    with pytest.raises(LoaderError, match="requires @target-doc"):
        load_document(doc_folder)


def test_reject_unknown_attribute_kind(doc_folder: Path) -> None:
    manifest = doc_folder / "DOC.xml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'kind="text"', 'kind="weird"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(LoaderError, match="unsupported kind"):
        load_document(doc_folder)


@pytest.mark.parametrize("kind", ["integer", "real", "boolean", "date"])
def test_load_non_string_attribute_kinds(doc_folder: Path, kind: str) -> None:
    manifest = doc_folder / "DOC.xml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            '<attribute name="rationale" kind="text" required="false"/>',
            f'<attribute name="rationale" kind="{kind}" required="false"/>',
        ),
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    attr = doc.attribute("rationale")
    assert attr is not None
    assert attr.kind == kind
    assert attr.options == ()


def test_reject_options_on_non_enum(doc_folder: Path) -> None:
    manifest = doc_folder / "DOC.xml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            '<attribute name="rationale" kind="text" required="false"/>',
            '<attribute name="rationale" kind="integer" required="false">'
            "<option>1</option></attribute>",
        ),
        encoding="utf-8",
    )
    with pytest.raises(LoaderError, match="cannot declare <option>s"):
        load_document(doc_folder)


def test_load_document_without_views(doc_folder: Path) -> None:
    doc = load_document(doc_folder)
    assert doc.views == ()


def test_load_document_with_views(doc_folder: Path) -> None:
    views_dir = doc_folder / "views"
    views_dir.mkdir()
    (views_dir / "main-view.xml").write_text(
        """<?xml version="1.0"?>
<view name="Main View">
  <attribute name="status"/>
  <attribute name="type"/>
</view>
""",
        encoding="utf-8",
    )
    (views_dir / "qa-view.xml").write_text(
        """<?xml version="1.0"?>
<view name="QA View">
  <attribute name="rationale"/>
</view>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert [v.slug for v in doc.views] == ["main-view", "qa-view"]
    assert doc.views[0].name == "Main View"
    assert doc.views[0].visible == ("status", "type")
    assert doc.views[1].name == "QA View"
    assert doc.views[1].visible == ("rationale",)


def test_view_missing_name_attribute_rejected(doc_folder: Path) -> None:
    views_dir = doc_folder / "views"
    views_dir.mkdir()
    (views_dir / "bad.xml").write_text(
        '<?xml version="1.0"?><view><attribute name="status"/></view>\n',
        encoding="utf-8",
    )
    with pytest.raises(LoaderError, match="missing required @name"):
        load_document(doc_folder)


def test_view_with_duplicate_attribute_rejected(doc_folder: Path) -> None:
    views_dir = doc_folder / "views"
    views_dir.mkdir()
    (views_dir / "dup.xml").write_text(
        '<?xml version="1.0"?>\n'
        '<view name="Dup">\n'
        '  <attribute name="status"/>\n'
        '  <attribute name="status"/>\n'
        "</view>\n",
        encoding="utf-8",
    )
    with pytest.raises(LoaderError, match="duplicate <attribute"):
        load_document(doc_folder)


# --- filters --------------------------------------------------------------


def test_load_document_without_filters(doc_folder: Path) -> None:
    doc = load_document(doc_folder)
    assert doc.filters == ()


def test_load_document_with_filters(doc_folder: Path) -> None:
    filters_dir = doc_folder / "filters"
    filters_dir.mkdir()
    (filters_dir / "high-pri.xml").write_text(
        """<?xml version="1.0"?>
<filter name="High priority" combinator="all" keep-ancestors="true">
  <rule kind="attr" attr="status" op="eq" value="Approved"/>
  <rule kind="has-outgoing" attr="PARENT" op="present"/>
</filter>
""",
        encoding="utf-8",
    )
    (filters_dir / "broken.xml").write_text(
        """<?xml version="1.0"?>
<filter name="Anything" combinator="any" keep-ancestors="false">
  <rule kind="attr" attr="rationale" op="is-empty"/>
  <rule kind="has-incoming" op="absent"/>
</filter>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert [f.slug for f in doc.filters] == ["broken", "high-pri"]
    high = doc.filters[1]
    assert high.name == "High priority"
    assert high.combinator == "all"
    assert high.keep_ancestors is True
    assert len(high.rules) == 2
    assert high.rules[0].kind == "attr"
    assert high.rules[0].attr == "status"
    assert high.rules[0].op == "eq"
    assert high.rules[0].value == "Approved"
    assert high.rules[1].kind == "has-outgoing"
    assert high.rules[1].attr == "PARENT"
    assert high.rules[1].op == "present"
    broken = doc.filters[0]
    assert broken.combinator == "any"
    assert broken.keep_ancestors is False


def test_filter_unknown_combinator_rejected(doc_folder: Path) -> None:
    filters_dir = doc_folder / "filters"
    filters_dir.mkdir()
    (filters_dir / "bad.xml").write_text(
        '<?xml version="1.0"?>\n'
        '<filter name="Bad" combinator="maybe">\n'
        '  <rule kind="attr" attr="status" op="eq" value="x"/>\n'
        "</filter>\n",
        encoding="utf-8",
    )
    with pytest.raises(LoaderError, match="combinator must be"):
        load_document(doc_folder)


def test_filter_unknown_kind_rejected(doc_folder: Path) -> None:
    filters_dir = doc_folder / "filters"
    filters_dir.mkdir()
    (filters_dir / "bad.xml").write_text(
        '<?xml version="1.0"?>\n'
        '<filter name="Bad">\n'
        '  <rule kind="weird" attr="status" op="eq" value="x"/>\n'
        "</filter>\n",
        encoding="utf-8",
    )
    with pytest.raises(LoaderError, match="kind must be one of"):
        load_document(doc_folder)


# --- tables ---------------------------------------------------------------


def _write_table_object(doc_folder: Path, table_xml: str) -> None:
    (doc_folder / "objects" / "DOC-1.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<object id="DOC-1">\n'
        '  <attr name="type">Information</attr>\n'
        '  <attr name="status">Draft</attr>\n'
        "  <description>Caption</description>\n"
        f"{table_xml}\n"
        "</object>\n",
        encoding="utf-8",
    )


def test_object_without_table_has_none(doc_folder: Path) -> None:
    assert load_document(doc_folder).objects["DOC-1"].table is None


def test_parse_table_basic(doc_folder: Path) -> None:
    _write_table_object(
        doc_folder,
        "  <table>\n"
        '    <columns><col align="left"/><col align="right"/></columns>\n'
        "    <row><cell>Signal</cell><cell>Latency</cell></row>\n"
        "  </table>",
    )
    table = load_document(doc_folder).objects["DOC-1"].table
    assert table is not None
    assert [c.align for c in table.columns] == ["left", "right"]
    assert [c.text for c in table.rows[0].cells] == ["Signal", "Latency"]


def test_parse_table_header_and_spans(doc_folder: Path) -> None:
    _write_table_object(
        doc_folder,
        "  <table>\n"
        "    <columns><col/><col/></columns>\n"
        '    <row header="true"><cell>H1</cell><cell>H2</cell></row>\n'
        '    <row><cell colspan="2">spanned</cell></row>\n'
        "  </table>",
    )
    table = load_document(doc_folder).objects["DOC-1"].table
    assert table.rows[0].header is True
    assert table.rows[1].cells[0].colspan == 2


def test_parse_table_default_align(doc_folder: Path) -> None:
    _write_table_object(
        doc_folder,
        "  <table><columns><col/></columns><row><cell>x</cell></row></table>",
    )
    assert load_document(doc_folder).objects["DOC-1"].table.columns[0].align == "left"


def test_parse_table_bad_align_raises(doc_folder: Path) -> None:
    _write_table_object(
        doc_folder,
        '  <table><columns><col align="middle"/></columns>'
        "<row><cell>x</cell></row></table>",
    )
    with pytest.raises(LoaderError, match="align"):
        load_document(doc_folder)


def test_parse_table_non_integer_span_raises(doc_folder: Path) -> None:
    _write_table_object(
        doc_folder,
        '  <table><columns><col/></columns>'
        '<row><cell colspan="two">x</cell></row></table>',
    )
    with pytest.raises(LoaderError, match="colspan"):
        load_document(doc_folder)


def test_parse_table_inconsistent_geometry_raises(doc_folder: Path) -> None:
    _write_table_object(
        doc_folder,
        "  <table><columns><col/><col/></columns>"
        "<row><cell>only-one</cell></row></table>",
    )
    with pytest.raises(LoaderError, match="geometry"):
        load_document(doc_folder)


def test_filter_attr_rule_unknown_op_rejected(doc_folder: Path) -> None:
    filters_dir = doc_folder / "filters"
    filters_dir.mkdir()
    (filters_dir / "bad.xml").write_text(
        '<?xml version="1.0"?>\n'
        '<filter name="Bad">\n'
        '  <rule kind="attr" attr="status" op="strange" value="x"/>\n'
        "</filter>\n",
        encoding="utf-8",
    )
    with pytest.raises(LoaderError, match="recognised operator"):
        load_document(doc_folder)


def test_filter_link_rule_op_must_be_present_or_absent(doc_folder: Path) -> None:
    filters_dir = doc_folder / "filters"
    filters_dir.mkdir()
    (filters_dir / "bad.xml").write_text(
        '<?xml version="1.0"?>\n'
        '<filter name="Bad">\n'
        '  <rule kind="has-outgoing" op="contains" value="x"/>\n'
        "</filter>\n",
        encoding="utf-8",
    )
    with pytest.raises(LoaderError, match="op must be one of"):
        load_document(doc_folder)


def test_filter_missing_name_rejected(doc_folder: Path) -> None:
    filters_dir = doc_folder / "filters"
    filters_dir.mkdir()
    (filters_dir / "bad.xml").write_text(
        '<?xml version="1.0"?>\n'
        '<filter combinator="all"/>\n',
        encoding="utf-8",
    )
    with pytest.raises(LoaderError, match="missing required @name"):
        load_document(doc_folder)


def test_filter_attr_rule_missing_attr_rejected(doc_folder: Path) -> None:
    filters_dir = doc_folder / "filters"
    filters_dir.mkdir()
    (filters_dir / "bad.xml").write_text(
        '<?xml version="1.0"?>\n'
        '<filter name="Bad">\n'
        '  <rule kind="attr" op="eq" value="x"/>\n'
        "</filter>\n",
        encoding="utf-8",
    )
    with pytest.raises(LoaderError, match="missing @attr"):
        load_document(doc_folder)


def test_filter_keep_ancestors_defaults_true(doc_folder: Path) -> None:
    filters_dir = doc_folder / "filters"
    filters_dir.mkdir()
    (filters_dir / "minimal.xml").write_text(
        '<?xml version="1.0"?>\n'
        '<filter name="Minimal"/>\n',
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert doc.filters[0].keep_ancestors is True
    assert doc.filters[0].combinator == "all"
    assert doc.filters[0].rules == ()


# --- figures --------------------------------------------------------------


_LOADER_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">
  <rect width="10" height="10"/>
</svg>
"""


def test_figure_path_none_when_no_figures(doc_folder: Path) -> None:
    doc = load_document(doc_folder)
    assert doc.objects["DOC-0"].figure_path is None
    assert doc.objects["DOC-1"].figure_path is None


def test_figure_path_populated_when_file_present(doc_folder: Path) -> None:
    figures_dir = doc_folder / "figures"
    figures_dir.mkdir()
    (figures_dir / "DOC-1.svg").write_bytes(_LOADER_SVG)
    doc = load_document(doc_folder)
    assert doc.objects["DOC-1"].figure_path == "figures/DOC-1.svg"
    assert doc.objects["DOC-0"].figure_path is None


def test_figure_path_ignores_orphan_svgs(doc_folder: Path) -> None:
    figures_dir = doc_folder / "figures"
    figures_dir.mkdir()
    (figures_dir / "DOC-99.svg").write_bytes(_LOADER_SVG)  # no such object
    doc = load_document(doc_folder)
    # Should not raise, should not populate any object's figure_path.
    assert all(o.figure_path is None for o in doc.objects.values())

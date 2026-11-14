"""Tests for schema helpers — currently the table geometry validator."""

from __future__ import annotations

import pytest

from mellon.schema import (
    Table,
    TableCell,
    TableColumn,
    TableRow,
    is_document_id,
    is_object_id,
    table_geometry_errors,
)


def _cols(n: int) -> tuple[TableColumn, ...]:
    return tuple(TableColumn() for _ in range(n))


def _row(*texts: str, header: bool = False) -> TableRow:
    return TableRow(cells=tuple(TableCell(text=t) for t in texts), header=header)


def test_simple_rectangle_ok() -> None:
    t = Table(_cols(2), (_row("a", "b", header=True), _row("c", "d")))
    assert table_geometry_errors(t) == []


def test_colspan_fills_row_ok() -> None:
    t = Table(_cols(3), (TableRow((TableCell("wide", colspan=3),)),))
    assert table_geometry_errors(t) == []


def test_rowspan_ok() -> None:
    t = Table(
        _cols(2),
        (
            TableRow((TableCell("a", rowspan=2), TableCell("b"))),
            TableRow((TableCell("c"),)),  # col 0 covered by the rowspan above
        ),
    )
    assert table_geometry_errors(t) == []


def test_colspan_plus_rowspan_ok() -> None:
    # A 2x2 block in the top-left, then the remaining column.
    t = Table(
        _cols(3),
        (
            TableRow((TableCell("block", colspan=2, rowspan=2), TableCell("r0c2"))),
            TableRow((TableCell("r1c2"),)),
        ),
    )
    assert table_geometry_errors(t) == []


def test_colspan_overflow_flagged() -> None:
    t = Table(_cols(2), (TableRow((TableCell("x", colspan=3),)),))
    errs = table_geometry_errors(t)
    assert errs and "overflow" in errs[0]


def test_short_row_flagged() -> None:
    t = Table(_cols(3), (_row("only-one"),))
    errs = table_geometry_errors(t)
    assert errs and "too few cells" in errs[0]


def test_extra_cells_flagged() -> None:
    t = Table(_cols(2), (_row("a", "b", "c"),))
    errs = table_geometry_errors(t)
    assert errs and "beyond table width" in errs[0]


def test_rowspan_past_bottom_flagged() -> None:
    t = Table(_cols(1), (TableRow((TableCell("x", rowspan=3),)),))
    errs = table_geometry_errors(t)
    assert errs and "past the" in errs[0]


def test_no_columns_with_rows_flagged() -> None:
    t = Table((), (_row(),))
    errs = table_geometry_errors(t)
    assert errs and "no <col>" in errs[0]


def test_overlapping_spans_offset_ok() -> None:
    # Row 0: a rowspan in col 0 + a normal cell in col 1.
    # Row 1: col 0 is carried, so a SINGLE explicit cell fills col 1.
    t = Table(
        _cols(2),
        (
            TableRow((TableCell("a", rowspan=2), TableCell("b"))),
            TableRow((TableCell("c"),)),
        ),
    )
    assert table_geometry_errors(t) == []
    # But two cells in row 1 would overrun (col 0 is already carried).
    bad = Table(
        _cols(2),
        (
            TableRow((TableCell("a", rowspan=2), TableCell("b"))),
            TableRow((TableCell("c"), TableCell("d"))),
        ),
    )
    assert table_geometry_errors(bad)


def test_empty_table_ok() -> None:
    assert table_geometry_errors(Table()) == []


# --- document and object IDs (docs/definitions.md) ---


@pytest.mark.parametrize("text", ["DOC", "MELLON-STORE-SRS", "a_b-9", "CONSOLE", "COM10", "LPT0"])
def test_is_document_id_accepts(text: str) -> None:
    assert is_document_id(text)


@pytest.mark.parametrize(
    "text",
    ["", "../DOC", "DOC/x", "\\\\host\\share", "C:", "DOC ", "DOC\n", "DÖC", "DOC²",
     "con", "CON", "Nul", "com1", "LPT9", None, 7],
)
def test_is_document_id_refuses(text) -> None:
    assert not is_document_id(text)


@pytest.mark.parametrize(
    ("doc_id", "obj_id"),
    [("DOC", "DOC-0"), ("DOC", "DOC-1"), ("A-B", "A-B-12"), ("DOC", "DOC-007")],
)
def test_is_object_id_accepts(doc_id: str, obj_id: str) -> None:
    assert is_object_id(doc_id, obj_id)


@pytest.mark.parametrize(
    ("doc_id", "obj_id"),
    [
        ("DOC", "DOC-"),
        ("DOC", "DOC-1a"),
        ("DOC", "DOC-²"),
        ("DOC", "DOC-1\n"),
        ("DOC", "OTHER-1"),
        ("DOC", "DOC1"),
        ("DOC", "DOC-1/../../x"),
        ("DOC", "../DOC-1"),
        ("../DOC", "../DOC-1"),
        ("CON", "CON-1"),
        ("DOC", None),
    ],
)
def test_is_object_id_refuses(doc_id, obj_id) -> None:
    assert not is_object_id(doc_id, obj_id)


# --- code view findings (CV-STORE-*) ---


def test_store_srs_111_113_cell_spanning_over_a_row_span_is_an_error(tmp_path):
    """CV-STORE-01, MELLON-STORE-SRS-111 and -113: three columns; row 1 = A, B
    (rowspan 2), C; row 2 = D (colspan 2), E; row 3 = F, G. D, placed at column 0,
    spans over column 1, which B still covers, so (2,1) is covered twice and (3,1) by
    nothing. The table doesn't tile its grid: a geometry error and a load error."""
    from mellon.loader import LoaderError, load_document
    from mellon.schema import (Table, TableCell, TableColumn, TableRow,
                               table_geometry_errors)

    table = Table(
        columns=(TableColumn(), TableColumn(), TableColumn()),
        rows=(
            TableRow(cells=(TableCell("A"), TableCell("B", rowspan=2), TableCell("C"))),
            TableRow(cells=(TableCell("D", colspan=2), TableCell("E"))),
            TableRow(cells=(TableCell("F"), TableCell("G"))),
        ),
    )
    assert table_geometry_errors(table)

    doc = tmp_path / "DOC"
    (doc / "objects").mkdir(parents=True)
    (doc / "DOC.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<document id="DOC">\n'
        '  <outline><ref id="DOC-1"/></outline>\n</document>\n', encoding="utf-8")
    (doc / "objects" / "DOC-1.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<object id="DOC-1">\n'
        "  <description>t</description>\n  <table>\n"
        '    <columns><col/><col/><col/></columns>\n'
        '    <row><cell>A</cell><cell rowspan="2">B</cell><cell>C</cell></row>\n'
        '    <row><cell colspan="2">D</cell><cell>E</cell></row>\n'
        "    <row><cell>F</cell><cell>G</cell></row>\n"
        "  </table>\n</object>\n", encoding="utf-8")
    with pytest.raises(LoaderError, match="geometry"):
        load_document(doc)

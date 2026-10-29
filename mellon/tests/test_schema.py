"""Tests for schema helpers — currently the table geometry validator."""

from __future__ import annotations

from mellon.schema import (
    Table,
    TableCell,
    TableColumn,
    TableRow,
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

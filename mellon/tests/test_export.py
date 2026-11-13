"""Tests for src/mellon/export.py — LaTeX/PDF export."""

from __future__ import annotations

import dataclasses
import io
import re
import subprocess
import sys
import zlib
from pathlib import Path

import pytest
from pypdf import PdfReader

from mellon import Table, TableCell, TableColumn, TableRow, set_table
from mellon import export
from mellon.export import (
    _LATEX_ESCAPE,
    _find_tectonic,
    build_pdf,
    escape_latex,
    pdf,
    render_latex,
    render_table_latex,
    unrendered_figures,
)
from mellon.loader import load_document
from mellon.schema import Document, Metadata, OutlineNode, RequirementObject

_needs_tectonic = pytest.mark.skipif(
    _find_tectonic() is None,
    reason="tectonic not on PATH and not in <project-root>/tools/",
)


def test_escape_latex_specials():
    """Each LaTeX-special char round-trips through the escape map."""
    for needle, replacement in _LATEX_ESCAPE.items():
        assert escape_latex(needle) == replacement
    assert escape_latex("a & b") == r"a \& b"
    assert escape_latex("100% pure") == r"100\% pure"
    # Single-pass translation: the `{}` inside `\textbackslash{}` must not
    # itself get re-escaped, so an input backslash followed by `&` produces
    # exactly two escaped tokens, not three.
    assert escape_latex(r"\& lookup") == r"\textbackslash{}\& lookup"


def test_render_latex_includes_metadata_and_outline(doc_folder: Path):
    doc = load_document(doc_folder)
    tex = render_latex(doc)

    # Uses the bundled syrs document class.
    assert r"\documentclass{syrs}" in tex
    # Doc metadata flows into the cover-page setters.
    assert r"\setDocID{DOC}" in tex
    assert r"\setDocSubject{Test doc}" in tex
    assert r"\setDocOwner{tester}" in tex
    assert r"\setDocDate{2026-04-21}" in tex
    # No rev passed → no \setDocRev line; class default stands.
    assert r"\setDocRev" not in tex
    # DOC-0 is a Heading at outline depth 1 → \syrssection{description}.
    assert r"\syrssection{Top heading}" in tex
    # DOC-1 is a Functional object → wrapped in the `requirement` env.
    assert r"\begin{requirement}{DOC-1}{}" in tex
    assert r"\shall{The system shall do X.}" in tex
    # Boilerplate present.
    assert r"\begin{document}" in tex
    assert r"\end{document}" in tex


def test_render_latex_rev_kwarg(doc_folder: Path):
    """Passing `rev` emits \\setDocRev with the given value."""
    doc = load_document(doc_folder)
    tex = render_latex(doc, rev="C.04")
    assert r"\setDocRev{C.04}" in tex


def test_render_latex_attribute_subset(doc_folder: Path):
    doc = load_document(doc_folder)

    # Default attr subset doesn't include "rationale", so DOC-1's rationale
    # value ("because") must NOT render.
    tex_default = render_latex(doc)
    assert "rationale" not in tex_default
    assert "because" not in tex_default

    # Asking for it explicitly renders it as an \attrcell{K}{V} cell
    # inside the \attrs row.
    tex_with = render_latex(doc, attrs=("type", "status", "rationale"))
    assert r"\attrcell{rationale}{because}" in tex_with
    assert r"\attrs{" in tex_with


@_needs_tectonic
def test_pdf_compiles_for_fixture_doc(doc_folder: Path):
    doc = load_document(doc_folder)
    blob = pdf(doc)
    assert blob.startswith(b"%PDF-")
    # A real PDF for even a tiny doc is well over 1 KB.
    assert len(blob) > 1000


def test_build_pdf_writes_class_file_alongside_tex(
    monkeypatch: pytest.MonkeyPatch,
):
    """`syrs.cls` must be staged in the temp dir before tectonic runs.

    Otherwise `\\documentclass{syrs}` won't resolve. We monkeypatch
    subprocess.run to fail fast and just inspect what landed in the dir.
    """
    monkeypatch.setattr(export, "_find_tectonic", lambda: "tectonic")

    seen: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001 — mirroring stdlib
        # Locate the temp dir from the --outdir argument.
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        seen["files"] = sorted(p.name for p in outdir.iterdir())
        return type(
            "R",
            (),
            {"returncode": 1, "stdout": "", "stderr": "stub"},
        )()

    monkeypatch.setattr(export.subprocess, "run", fake_run)

    with pytest.raises(export.TectonicError):
        build_pdf(r"\documentclass{syrs}\begin{document}hi\end{document}")

    assert "syrs.cls" in seen["files"]
    assert "doc.tex" in seen["files"]


def test_build_pdf_raises_when_tectonic_missing(monkeypatch: pytest.MonkeyPatch):
    """If tectonic isn't reachable, build_pdf raises a clear error."""
    monkeypatch.setattr(export, "_find_tectonic", lambda: None)
    with pytest.raises(export.TectonicError, match="tectonic binary not found"):
        build_pdf(r"\documentclass{article}\begin{document}hi\end{document}")


# --- tables ---------------------------------------------------------------


def test_render_table_latex_basic():
    t = Table(
        columns=(TableColumn("left"), TableColumn("right")),
        rows=(
            TableRow((TableCell("Signal"), TableCell("Latency")), header=True),
            TableRow((TableCell("OVERTEMP"), TableCell("200 ms"))),
        ),
    )
    tex = render_table_latex(t)
    assert r"\begin{longtable}{lr}" in tex
    assert r"\toprule" in tex and r"\midrule" in tex and r"\bottomrule" in tex
    # Header cell is bolded and sits above the midrule.
    assert tex.index(r"\textbf{Signal}") < tex.index(r"\midrule")


def test_render_table_latex_repeats_leading_header_rows():
    """Leading header rows end the longtable head, so they repeat on every
    page the table spans."""
    t = Table(
        columns=(TableColumn(), TableColumn()),
        rows=(
            TableRow((TableCell("Group", colspan=2),), header=True),
            TableRow((TableCell("Signal"), TableCell("Latency")), header=True),
            TableRow((TableCell("OVERTEMP"), TableCell("200 ms"))),
        ),
    )
    lines = render_table_latex(t).splitlines()
    head_end = lines.index(r"\endhead")
    assert lines[head_end - 1] == r"\midrule"
    assert r"\textbf{Latency}" in lines[head_end - 2]
    assert "OVERTEMP" in lines[head_end + 1]


@pytest.mark.parametrize(
    "rows",
    [
        # Only header rows: no body for the head to repeat over.
        (TableRow((TableCell("a"), TableCell("b")), header=True),),
        # A header row below the body isn't a page head.
        (
            TableRow((TableCell("a"), TableCell("b"))),
            TableRow((TableCell("c"), TableCell("d")), header=True),
        ),
        # A header cell whose rowspan reaches into the body.
        (
            TableRow((TableCell("tall", rowspan=2), TableCell("b")), header=True),
            TableRow((TableCell("c"),)),
        ),
        # The same, with a later header row and body row after it.
        (
            TableRow((TableCell("tall", rowspan=2), TableCell("b")), header=True),
            TableRow((TableCell("c"),)),
            TableRow((TableCell("e"), TableCell("f")), header=True),
            TableRow((TableCell("g"), TableCell("h"))),
        ),
        # No header rows at all.
        (TableRow((TableCell("a"), TableCell("b"))),),
    ],
    ids=["all-header", "trailing", "rowspan-into-body", "rowspan-into-body-then-split", "none"],
)
def test_render_table_latex_no_repeating_head(rows):
    t = Table(columns=(TableColumn(), TableColumn()), rows=rows)
    assert r"\endhead" not in render_table_latex(t)


def test_render_table_latex_repeats_leading_header_rows_before_a_later_header_row():
    """The header rows before the first body row repeat even when a later
    header row follows a body row (MELLON-PDF-SRS-147). The later one stays a
    bold row in the body, with its own rule below it."""
    t = Table(
        columns=(TableColumn(), TableColumn()),
        rows=(
            TableRow((TableCell("a"), TableCell("b")), header=True),
            TableRow((TableCell("c"), TableCell("d"))),
            TableRow((TableCell("e"), TableCell("f")), header=True),
            TableRow((TableCell("g"), TableCell("h"))),
        ),
    )
    lines = render_table_latex(t).splitlines()
    assert lines.count(r"\endhead") == 1
    head_end = lines.index(r"\endhead")
    assert lines[head_end - 1] == r"\midrule"
    assert r"\textbf{a}" in lines[head_end - 2]
    assert lines[head_end + 1].strip().startswith("c & d")
    later = next(i for i, line in enumerate(lines) if r"\textbf{e}" in line)
    assert later > head_end
    assert lines[later + 1] == r"\midrule"


def test_render_table_latex_colspan():
    t = Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow((TableCell("wide", colspan=2),)),),
    )
    assert r"\multicolumn{2}" in render_table_latex(t)


def test_render_table_latex_rowspan():
    t = Table(
        columns=(TableColumn(), TableColumn()),
        rows=(
            TableRow((TableCell("tall", rowspan=2), TableCell("b"))),
            TableRow((TableCell("c"),)),
        ),
    )
    tex = render_table_latex(t)
    assert r"\multirow{2}{*}{tall}" in tex
    # The covered slot in row 2 is a blank leading entry: "   & c".
    body_lines = [ln for ln in tex.splitlines() if "&" in ln]
    assert any(ln.strip().startswith("& c") for ln in body_lines)


def test_render_table_latex_escapes_cell_text():
    t = Table(
        columns=(TableColumn(),),
        rows=(TableRow((TableCell("100% & rising_fast"),)),),
    )
    tex = render_table_latex(t)
    assert r"100\% \& rising\_fast" in tex


def test_render_latex_includes_table_object(doc_folder: Path):
    set_table(
        doc_folder,
        "DOC-1",
        Table(
            columns=(TableColumn("left"), TableColumn("right")),
            rows=(TableRow((TableCell("Signal"), TableCell("Latency")), header=True),),
        ),
    )
    tex = render_latex(load_document(doc_folder))
    assert r"\begin{longtable}" in tex
    # Caption (the description) renders bold...
    assert r"\textbf{The system shall do X.}" in tex
    # ...and the table object is NOT wrapped as a requirement card.
    assert r"\begin{requirement}{DOC-1}" not in tex


def test_render_table_latex_guards_leading_bracket_and_star():
    """A row starting with `[` or `*` must not be eaten by `\\\\` / `\\toprule`.

    Both look ahead (past whitespace) for an optional `[...]` / `*`; an empty
    group in front of the row ends that lookahead.
    """
    t = Table(
        columns=(TableColumn(), TableColumn()),
        rows=(
            TableRow((TableCell("[1] first"), TableCell("x"))),
            TableRow((TableCell("* starred"), TableCell("y"))),
            TableRow((TableCell(" [2] padded"), TableCell("z"))),
            TableRow((TableCell("plain"), TableCell("[3] not first"))),
        ),
    )
    lines = render_table_latex(t).splitlines()
    # Cell text is collapsed to one line (leading space dropped) and guarded.
    assert r"  {}[1] first & x \\" in lines
    assert r"  {}* starred & y \\" in lines
    assert r"  {}[2] padded & z \\" in lines
    # Only a row's first cell is at risk; elsewhere the text is untouched.
    assert r"  plain & [3] not first \\" in lines


def test_render_table_latex_collapses_newlines_in_cells():
    """A blank line in a cell is a `\\par`, which breaks `\\textbf` (header)."""
    t = Table(
        columns=(TableColumn(),),
        rows=(TableRow((TableCell("two\n\nlines"),), header=True),),
    )
    assert r"\textbf{two lines}" in render_table_latex(t)


# --- one-line text: headings, captions, metadata ----------------------------


def _replace_in(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_render_latex_collapses_newlines_in_headings_and_captions(
    doc_folder: Path,
):
    """Blank lines in a heading break `\\addcontentsline`; in a table caption
    they break `\\textbf`. Both render on one line."""
    _replace_in(
        doc_folder / "objects" / "DOC-0.xml", "Top heading", "Top\n\n   heading"
    )
    set_table(
        doc_folder,
        "DOC-1",
        Table(columns=(TableColumn(),), rows=(TableRow((TableCell("x"),)),)),
    )
    _replace_in(
        doc_folder / "objects" / "DOC-1.xml",
        "The system shall do X.",
        "Caption\n\nline two",
    )
    tex = render_latex(load_document(doc_folder))
    assert r"\syrssection{Top heading}" in tex
    assert r"\textbf{Caption line two}" in tex


def test_render_latex_collapses_newlines_in_metadata(doc_folder: Path):
    manifest = doc_folder / "DOC.xml"
    _replace_in(manifest, "<title>Test doc</title>", "<title>Test\n\ndoc</title>")
    _replace_in(manifest, "<author>tester</author>", "<author>a\n\nb</author>")
    tex = render_latex(load_document(doc_folder), rev="A\n\n1")
    assert r"\setDocSubject{Test doc}" in tex
    assert r"\setDocOwner{a b}" in tex
    assert r"\setDocRev{A 1}" in tex


# --- figures ----------------------------------------------------------------


def _attach_figure(doc_folder: Path, obj_id: str) -> None:
    figures = doc_folder / "figures"
    figures.mkdir(exist_ok=True)
    (figures / f"{obj_id}.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8"
    )


def test_unrendered_figures_lists_exported_objects_with_figures(
    doc_folder: Path,
):
    assert unrendered_figures(load_document(doc_folder)) == []
    _attach_figure(doc_folder, "DOC-1")
    _attach_figure(doc_folder, "DOC-0")
    # A figure on an object outside the outline isn't exported, so it's
    # not reported either.
    (doc_folder / "objects" / "DOC-9.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<object id="DOC-9"><attr name="type">Information</attr>'
        "<description>orphan</description></object>\n",
        encoding="utf-8",
    )
    _attach_figure(doc_folder, "DOC-9")
    # Outline order.
    assert unrendered_figures(load_document(doc_folder)) == ["DOC-0", "DOC-1"]


def test_render_latex_marks_unrendered_figures(doc_folder: Path):
    """Each attached figure leaves a visible placeholder, never silence."""
    tex = render_latex(load_document(doc_folder))
    assert r"\figureplaceholder" not in tex

    _attach_figure(doc_folder, "DOC-1")
    _attach_figure(doc_folder, "DOC-0")
    tex = render_latex(load_document(doc_folder))
    # Requirement card: the placeholder sits inside the card.
    card = tex[tex.index(r"\begin{requirement}{DOC-1}") :]
    card = card[: card.index(r"\end{requirement}")]
    assert (
        r"\figureplaceholder{[Figure DOC-1.svg not rendered in PDF export]}"
        in card
    )
    # Heading: right after the heading macro.
    assert (
        "\\syrssection{Top heading}\n"
        r"\figureplaceholder{[Figure DOC-0.svg not rendered in PDF export]}"
        in tex
    )


def test_render_latex_figure_placeholder_on_table_object(doc_folder: Path):
    set_table(
        doc_folder,
        "DOC-1",
        Table(columns=(TableColumn(),), rows=(TableRow((TableCell("x"),)),)),
    )
    _attach_figure(doc_folder, "DOC-1")
    tex = render_latex(load_document(doc_folder))
    assert tex.index(r"\end{longtable}") < tex.index(
        r"\figureplaceholder{[Figure DOC-1.svg not rendered in PDF export]}"
    )


def test_render_latex_rendered_figure_in_card_takes_description_as_caption(
    doc_folder: Path,
):
    _attach_figure(doc_folder, "DOC-1")
    tex = render_latex(load_document(doc_folder), figures={"DOC-1": "fig-0.pdf"})
    card = tex[tex.index(r"\begin{requirement}{DOC-1}") :]
    card = card[: card.index(r"\end{requirement}")]
    # The figure replaces the \shall row; the description is its caption.
    assert r"\syrsfigure{fig-0.pdf}{The system shall do X.}" in card
    assert r"\shall" not in card
    assert r"\figureplaceholder" not in card
    # Attributes still print under it.
    assert r"\attrs{" in card


def test_render_latex_rendered_figure_on_heading_has_bare_number(doc_folder: Path):
    _attach_figure(doc_folder, "DOC-0")
    tex = render_latex(load_document(doc_folder), figures={"DOC-0": "fig-0.pdf"})
    assert "\\syrssection{Top heading}\n\\syrsfigure{fig-0.pdf}{}" in tex


def test_render_latex_rendered_figure_on_table_has_bare_number(doc_folder: Path):
    set_table(
        doc_folder,
        "DOC-1",
        Table(columns=(TableColumn(),), rows=(TableRow((TableCell("x"),)),)),
    )
    _attach_figure(doc_folder, "DOC-1")
    tex = render_latex(load_document(doc_folder), figures={"DOC-1": "fig-0.pdf"})
    assert tex.index(r"\end{longtable}") < tex.index(r"\syrsfigure{fig-0.pdf}{}")


def test_render_latex_figure_caption_is_one_escaped_line(doc_folder: Path):
    _replace_in(
        doc_folder / "objects" / "DOC-1.xml",
        "The system shall do X.",
        "Signal flow &amp; timing\n\n(100% load)",
    )
    _attach_figure(doc_folder, "DOC-1")
    tex = render_latex(load_document(doc_folder), figures={"DOC-1": "fig-0.pdf"})
    assert r"\syrsfigure{fig-0.pdf}{Signal flow \& timing (100\% load)}" in tex


def test_unrendered_figures_skips_rendered_ones(doc_folder: Path):
    _attach_figure(doc_folder, "DOC-0")
    _attach_figure(doc_folder, "DOC-1")
    doc = load_document(doc_folder)
    assert unrendered_figures(doc, {"DOC-1": "fig-1.pdf"}) == ["DOC-0"]


def _fake_drawio(monkeypatch, rendered: set[int]) -> list[list[Path]]:
    """draw.io "installed": renders the figures at the given indices."""
    calls: list[list[Path]] = []

    def fake_export(exe, svgs, out_dir):
        calls.append(list(svgs))
        out = {}
        for n in rendered:
            out[n] = Path(out_dir) / f"fig-{n}.pdf"
            out[n].write_bytes(b"%PDF-figure")
        return out

    monkeypatch.setattr(export.drawio, "find_drawio", lambda: "draw.io.exe")
    monkeypatch.setattr(export.drawio, "export_pdfs", fake_export)
    return calls


def _capture_build(monkeypatch) -> dict:
    seen: dict = {}

    def fake_build(tex, assets=None):
        seen["tex"] = tex
        seen["assets"] = {n: Path(p).read_bytes() for n, p in (assets or {}).items()}
        return b"%PDF-doc"

    monkeypatch.setattr(export, "build_pdf", fake_build)
    return seen


def test_pdf_renders_figures_through_drawio(doc_folder: Path, monkeypatch):
    _attach_figure(doc_folder, "DOC-0")
    _attach_figure(doc_folder, "DOC-1")
    calls = _fake_drawio(monkeypatch, rendered={1})  # DOC-0's figure fails
    seen = _capture_build(monkeypatch)
    missing: list[str] = []

    pdf(load_document(doc_folder), folder=doc_folder, unrendered=missing)

    # One draw.io run over the exported figures, in outline order.
    assert calls == [[doc_folder / "figures" / "DOC-0.svg",
                      doc_folder / "figures" / "DOC-1.svg"]]
    assert r"\syrsfigure{fig-1.pdf}{The system shall do X.}" in seen["tex"]
    assert seen["assets"] == {"fig-1.pdf": b"%PDF-figure"}
    assert r"\figureplaceholder{[Figure DOC-0.svg" in seen["tex"]
    assert missing == ["DOC-0"]


def test_pdf_without_drawio_keeps_placeholders(doc_folder: Path, monkeypatch):
    _attach_figure(doc_folder, "DOC-1")
    monkeypatch.setattr(export.drawio, "find_drawio", lambda: None)
    seen = _capture_build(monkeypatch)
    missing: list[str] = []

    pdf(load_document(doc_folder), folder=doc_folder, unrendered=missing)

    assert r"\syrsfigure" not in seen["tex"]
    assert seen["assets"] == {}
    assert missing == ["DOC-1"]


def test_pdf_does_not_start_drawio_without_figures(doc_folder: Path, monkeypatch):
    calls = _fake_drawio(monkeypatch, rendered=set())
    _capture_build(monkeypatch)
    pdf(load_document(doc_folder), folder=doc_folder)
    assert calls == []


def test_pdf_survives_drawio_failing_to_run(doc_folder: Path, monkeypatch):
    _attach_figure(doc_folder, "DOC-1")
    monkeypatch.setattr(export.drawio, "find_drawio", lambda: "bad.exe")

    def boom(exe, svgs, out_dir):
        raise export.drawio.DrawioError("could not run draw.io")

    monkeypatch.setattr(export.drawio, "export_pdfs", boom)
    seen = _capture_build(monkeypatch)
    missing: list[str] = []
    pdf(load_document(doc_folder), folder=doc_folder, unrendered=missing)
    assert r"\figureplaceholder" in seen["tex"]
    assert missing == ["DOC-1"]


def test_build_pdf_stages_assets_next_to_tex(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(export, "_find_tectonic", lambda: "tectonic")
    asset = tmp_path / "rendered.pdf"
    asset.write_bytes(b"%PDF-figure")
    seen: dict[str, bytes] = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001 — mirroring stdlib
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        seen["fig"] = (outdir / "fig-0.pdf").read_bytes()
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "stub"})()

    monkeypatch.setattr(export.subprocess, "run", fake_run)
    with pytest.raises(export.TectonicError):
        build_pdf("x", {"fig-0.pdf": asset})
    assert seen["fig"] == b"%PDF-figure"


@_needs_tectonic
def test_pdf_compiles_with_a_rendered_figure(doc_folder: Path, monkeypatch, tmp_path):
    """Through the real tectonic, with a real PDF as the "rendered" figure —
    one built by build_pdf itself, so the test needs no draw.io. The figure
    is far larger than the card, so it is scaled down to fit."""
    figure_pdf = tmp_path / "figure.pdf"
    figure_pdf.write_bytes(build_pdf(
        "\\documentclass{article}\\usepackage[paperwidth=40cm,paperheight=30cm]"
        "{geometry}\\pagestyle{empty}\\begin{document}Big diagram\\end{document}"
    ))
    _attach_figure(doc_folder, "DOC-1")

    def fake_export(exe, svgs, out_dir):
        out = Path(out_dir) / "fig-0.pdf"
        out.write_bytes(figure_pdf.read_bytes())
        return {0: out}

    monkeypatch.setattr(export.drawio, "find_drawio", lambda: "draw.io.exe")
    monkeypatch.setattr(export.drawio, "export_pdfs", fake_export)
    missing: list[str] = []
    blob = pdf(load_document(doc_folder), folder=doc_folder, unrendered=missing)
    assert blob.startswith(b"%PDF-")
    assert missing == []


# --- cover page / running header --------------------------------------------


def test_syrs_class_has_no_stamped_placeholders():
    """The class must not stamp ADAS/SyRS boilerplate or placeholder cover
    values on every export: every field defaults to empty and is hidden."""
    cls = export._read_bundled(export._CLASS_FILE)
    # Comments and the \ProvidesClass banner never reach the PDF.
    code = "\n".join(
        ln.split("%%")[0]
        for ln in cls.splitlines()
        if not ln.startswith(r"\ProvidesClass")
    )
    for stamped in (
        "ADAS",
        "System Requirements Specification",
        "Project Name",
        "Internal Use Only",
        "Document Owner",
        "29148",
        "DOC-SYRS-XXXX",
        "{QM}",
        "{A.00}",
        "{2026-01-01}",
    ):
        assert stamped not in code, stamped
    assert r"pdftitle={\@docSubject}" in code
    assert r"pdfauthor={\@docOwner}" in code


def test_render_latex_empty_title_sets_no_subject(doc_folder: Path):
    """An empty title is left out of the cover and the page headers; the
    document ID doesn't stand in for it (MELLON-PDF-SRS-82)."""
    _replace_in(doc_folder / "DOC.xml", "<title>Test doc</title>", "<title></title>")
    assert r"\setDocSubject" not in render_latex(load_document(doc_folder))


def _pdf_info_string(blob: bytes, key: str) -> str | None:
    """Read `/<key>` from a PDF's document info dict.

    xdvipdfmx writes that dict (recognisable by its `/Producer`; bookmark
    entries carry a `/Title` too) into a compressed object stream, with
    values as UTF-16 hex (`<feff...>`) or literal `(...)` strings.
    """
    info = re.compile(rb"<<((?:(?!<<|>>).)*?/Producer(?:(?!<<|>>).)*)>>", re.S)
    value = re.compile(
        rb"/" + key.encode()
        + rb"\s*(?:<([0-9A-Fa-f]+)>|\(((?:[^()\\]|\\.)*)\))"
    )
    chunks = [blob]
    for m in re.finditer(rb"stream\r?\n", blob):
        end = blob.find(b"endstream", m.end())
        try:
            chunks.append(zlib.decompress(blob[m.end() : end]))
        except zlib.error:
            continue
    for chunk in chunks:
        for d in info.finditer(chunk):
            m = value.search(d.group(1))
            if m is None:
                continue
            if m.group(1) is not None:
                return bytes.fromhex(m.group(1).decode()).decode("utf-16")
            return m.group(2).decode("latin-1")
    return None


@_needs_tectonic
def test_pdf_compiles_awkward_inputs_and_sets_pdf_info(doc_folder: Path):
    """End to end through the real tectonic: a heading with a blank line and
    a leading `[`, a table whose rows start with `[` / `*`, a caption and a
    header cell with blank lines, an attached figure, non-ASCII metadata,
    and a "₁" whose UTF-8 bytes in tectonic's output used to crash the
    cp1252 decode on Windows. pdftitle/pdfauthor come from the metadata."""
    manifest = doc_folder / "DOC.xml"
    _replace_in(
        manifest, "<title>Test doc</title>", "<title>Brake &amp; Steer åäö</title>"
    )
    _replace_in(manifest, "<author>tester</author>", "<author>T. Hällberg</author>")
    _replace_in(
        manifest, '<ref id="DOC-1"/>', '<ref id="DOC-1"/><ref id="DOC-2"/>'
    )
    _replace_in(
        doc_folder / "objects" / "DOC-0.xml",
        "Top heading",
        "[Draft] Top\n\nheading",
    )
    _replace_in(
        doc_folder / "objects" / "DOC-1.xml",
        "The system shall do X.",
        "The system shall keep V₁ below 5 V.",
    )
    _attach_figure(doc_folder, "DOC-1")
    (doc_folder / "objects" / "DOC-2.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<object id="DOC-2"><attr name="type">Information</attr>'
        '<attr name="status">Draft</attr>'
        "<description>Signal\n\ntable</description></object>\n",
        encoding="utf-8",
    )
    set_table(
        doc_folder,
        "DOC-2",
        Table(
            columns=(TableColumn("left"), TableColumn("right")),
            rows=(
                TableRow(
                    (TableCell("[unit]\n\nname"), TableCell("ms")), header=True
                ),
                TableRow((TableCell("[1] OVERTEMP"), TableCell("200"))),
                TableRow((TableCell("* derated"), TableCell("[400]"))),
            ),
        ),
    )
    doc = load_document(doc_folder)
    assert unrendered_figures(doc) == ["DOC-1"]

    blob = pdf(doc, rev="B.02")
    assert blob.startswith(b"%PDF-")
    assert _pdf_info_string(blob, "Title") == "Brake & Steer åäö"
    assert _pdf_info_string(blob, "Author") == "T. Hällberg"


@_needs_tectonic
def test_pdf_compiles_with_minimal_metadata(doc_folder: Path):
    """No title, rev, date or author: cover and header hide those fields
    instead of printing placeholders, and an empty first header line stays
    legal LaTeX. The PDF title falls back to the doc id."""
    manifest = doc_folder / "DOC.xml"
    _replace_in(manifest, "<title>Test doc</title>", "<title></title>")
    _replace_in(manifest, "<author>tester</author>", "<author></author>")
    _replace_in(manifest, "<date>2026-04-21</date>", "<date></date>")
    blob = pdf(load_document(doc_folder))
    assert blob.startswith(b"%PDF-")
    assert _pdf_info_string(blob, "Title") == "DOC"


# --- page numbers -----------------------------------------------------------


def _multipage_doc() -> Document:
    """Three headed sections of 25 requirement cards each: several pages
    per section, so each section starts on a page of its own."""
    objects: dict[str, RequirementObject] = {}
    outline = []
    for s in (1, 2, 3):
        heading = RequirementObject(f"DOC-{s}00", {"type": "Heading"}, f"Section {s}")
        objects[heading.id] = heading
        cards = []
        for n in range(1, 26):
            card = RequirementObject(
                f"DOC-{s}{n:02d}",
                {"type": "Requirement", "status": "Draft"},
                f"The system shall do thing {n} of section {s}. " * 3,
            )
            objects[card.id] = card
            cards.append(OutlineNode(card.id))
        outline.append(OutlineNode(heading.id, tuple(cards)))
    return Document(
        id="DOC",
        metadata=Metadata(title="Paging doc", author="tester", date="2026-04-21"),
        parents=(),
        attributes=(),
        outline=tuple(outline),
        objects=objects,
        links={},
    )


@pytest.fixture(scope="module")
def multipage_pdf_text() -> list[str]:
    """The text of each page of the exported `_multipage_doc`, in PDF order."""
    reader = PdfReader(io.BytesIO(pdf(_multipage_doc(), rev="1")))
    return [page.extract_text() for page in reader.pages]


_PAGE_NUMBER = re.compile(r"Page (\d+) of (\d+)")


@_needs_tectonic
def test_pdf_srs_104_pages_after_cover_show_their_number_and_the_total(
    multipage_pdf_text: list[str],
):
    """Page k of the PDF prints "Page k of N", N being the PDF's page count:
    the cover counts as page 1, so the table of contents is page 2."""
    total = len(multipage_pdf_text)
    assert total >= 6
    for k, text in enumerate(multipage_pdf_text[1:], start=2):
        assert _PAGE_NUMBER.findall(text) == [(str(k), str(total))], k
    # The cover may leave its number off, but a number it shows is right.
    assert set(_PAGE_NUMBER.findall(multipage_pdf_text[0])) <= {("1", str(total))}


@_needs_tectonic
def test_pdf_srs_104_contents_page_numbers_match_the_printed_page_numbers(
    multipage_pdf_text: list[str],
):
    """Each table-of-contents entry names the page its section heading is
    on, which is the number that page prints."""
    contents = multipage_pdf_text[1]
    entries = re.findall(
        r"^(\d+) (Section \d+)[ .]+?(\d+)$", contents, re.M
    )
    assert [(num, title) for num, title, _ in entries] == [
        ("1", "Section 1"), ("2", "Section 2"), ("3", "Section 3"),
    ]
    starts = []
    for num, title, listed in entries:
        heading = re.compile(rf"^{num}\. {title}$", re.M)
        on = [
            k for k, text in enumerate(multipage_pdf_text, start=1)
            if heading.search(text)
        ]
        assert len(on) == 1, (title, on)
        assert int(listed) == on[0], title
        assert _PAGE_NUMBER.findall(multipage_pdf_text[on[0] - 1])[0][0] == listed
        starts.append(on[0])
    # The sections start on different pages, so the check isn't vacuous.
    assert starts[0] == 3 and starts[0] < starts[1] < starts[2]


# --- tectonic lookup --------------------------------------------------------


@pytest.fixture
def clean_tectonic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every env var that steers tectonic lookup or its subprocess."""
    for var in ("MELLON_TECTONIC", "TECTONIC_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_find_tectonic_env_override_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_tectonic_env
):
    app = tmp_path / "Mellon"
    _touch(app / "tools" / "tectonic.exe")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "mellon.exe"))
    monkeypatch.setattr(export.shutil, "which", lambda _: "/usr/bin/tectonic")
    custom = str(tmp_path / "custom" / "tectonic.exe")
    monkeypatch.setenv("MELLON_TECTONIC", custom)
    # Returned as-is even though it doesn't exist: a wrong explicit path
    # should fail loudly at run time, not silently fall back.
    assert _find_tectonic() == custom


def test_find_tectonic_frozen_prefers_bundled_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_tectonic_env
):
    app = tmp_path / "Mellon"
    bundled = _touch(app / "tools" / "tectonic.exe")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "mellon.exe"))
    monkeypatch.setattr(export.shutil, "which", lambda _: "/usr/bin/tectonic")
    assert _find_tectonic() == str(bundled)

    # No bundled copy -> PATH, as before.
    bundled.unlink()
    assert _find_tectonic() == "/usr/bin/tectonic"


def test_find_tectonic_ignores_exe_tools_when_not_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_tectonic_env
):
    app = tmp_path / "python"
    _touch(app / "tools" / "tectonic.exe")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "python.exe"))
    monkeypatch.setattr(export.shutil, "which", lambda _: "/usr/bin/tectonic")
    assert _find_tectonic() == "/usr/bin/tectonic"


# --- tectonic subprocess ----------------------------------------------------


class _FakeRun:
    """Stand-in for `subprocess.run` that records every call.

    `returncodes` gives one exit code per expected call; a 0 drops a stub
    `doc.pdf` into `--outdir` so `build_pdf` finds its output.
    """

    def __init__(self, *returncodes: int) -> None:
        self.returncodes = list(returncodes)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kwargs):  # noqa: ANN001 — mirroring stdlib
        self.calls.append((cmd, kwargs))
        rc = self.returncodes.pop(0)
        if rc == 0:
            outdir = Path(cmd[cmd.index("--outdir") + 1])
            (outdir / "doc.pdf").write_bytes(b"%PDF-stub")
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="stub")

    def env(self, i: int) -> dict[str, str]:
        return self.calls[i][1]["env"]


_TEX = r"\documentclass{syrs}\begin{document}hi\end{document}"


@pytest.fixture
def fake_tectonic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_tectonic_env
) -> Path:
    """Point the lookup at a (non-existent) binary in `tmp_path/tools/`."""
    exe = tmp_path / "tools" / "tectonic.exe"
    monkeypatch.setattr(export, "_find_tectonic", lambda: str(exe))
    return exe


def test_build_pdf_subprocess_hygiene(
    monkeypatch: pytest.MonkeyPatch, fake_tectonic: Path
):
    """No console window, no inherited stdin, UTF-8 output, bounded time."""
    run = _FakeRun(0)
    monkeypatch.setattr(export.subprocess, "run", run)
    assert build_pdf(_TEX) == b"%PDF-stub"

    ((cmd, kwargs),) = run.calls
    assert cmd[0] == str(fake_tectonic)
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["capture_output"] is True
    # Not `text=True`: that decodes with the locale codepage (cp1252 on
    # Windows), which crashes on tectonic's UTF-8 output.
    assert "text" not in kwargs
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["timeout"] == export._TECTONIC_TIMEOUT_S == 300
    expected_flags = (
        subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    )
    assert kwargs["creationflags"] == expected_flags


def test_build_pdf_failure_raises_once(
    monkeypatch: pytest.MonkeyPatch, fake_tectonic: Path
):
    run = _FakeRun(1)
    monkeypatch.setattr(export.subprocess, "run", run)
    with pytest.raises(export.TectonicError, match="tectonic exited 1"):
        build_pdf(_TEX)
    assert len(run.calls) == 1


def test_build_pdf_cold_cache_offline_gets_a_hint(
    monkeypatch: pytest.MonkeyPatch, fake_tectonic: Path
):
    def offline(cmd, **kwargs):  # noqa: ANN001 — mirroring stdlib
        return subprocess.CompletedProcess(
            cmd, 101, stdout="",
            stderr="this bundle isn't cached, and we couldn't get it from "
            "the internet. Error: ...",
        )

    monkeypatch.setattr(export.subprocess, "run", offline)
    with pytest.raises(export.TectonicError, match="needs internet access"):
        build_pdf(_TEX)


def test_build_pdf_timeout_raises_tectonic_error(
    monkeypatch: pytest.MonkeyPatch, fake_tectonic: Path
):
    calls: list[list[str]] = []

    def hang(cmd, **kwargs):  # noqa: ANN001 — mirroring stdlib
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(export.subprocess, "run", hang)
    with pytest.raises(export.TectonicError, match="did not finish within 300 s"):
        build_pdf(_TEX)
    assert len(calls) == 1


def test_build_pdf_unrunnable_binary_raises_tectonic_error(
    monkeypatch: pytest.MonkeyPatch, fake_tectonic: Path
):
    def missing(cmd, **kwargs):  # noqa: ANN001 — mirroring stdlib
        raise FileNotFoundError(2, "No such file", cmd[0])

    monkeypatch.setattr(export.subprocess, "run", missing)
    with pytest.raises(export.TectonicError, match="could not run tectonic"):
        build_pdf(_TEX)


def test_build_pdf_uses_bundled_cache_dir(
    monkeypatch: pytest.MonkeyPatch, fake_tectonic: Path
):
    """`tectonic-cache/` next to the binary is passed to tectonic."""
    cache = fake_tectonic.parent / "tectonic-cache"
    cache.mkdir(parents=True)
    run = _FakeRun(0)
    monkeypatch.setattr(export.subprocess, "run", run)
    build_pdf(_TEX)
    assert run.env(0)["TECTONIC_CACHE_DIR"] == str(cache)


def test_build_pdf_cache_dir_respects_caller_and_absence(
    monkeypatch: pytest.MonkeyPatch, fake_tectonic: Path, tmp_path: Path
):
    run = _FakeRun(0, 0)
    monkeypatch.setattr(export.subprocess, "run", run)
    # No bundled cache folder -> nothing injected.
    build_pdf(_TEX)
    assert "TECTONIC_CACHE_DIR" not in run.env(0)
    # A caller-set cache dir wins over the bundled one.
    (fake_tectonic.parent / "tectonic-cache").mkdir(parents=True)
    monkeypatch.setenv("TECTONIC_CACHE_DIR", str(tmp_path / "mine"))
    build_pdf(_TEX)
    assert run.env(1)["TECTONIC_CACHE_DIR"] == str(tmp_path / "mine")


# --- Traceability section ---------------------------------------------------


def _parent_doc(*objects: RequirementObject) -> Document:
    """PARENT, the doc_folder fixture's declared parent: a heading over
    ``objects``."""
    heading = RequirementObject(
        "PARENT-0", {"type": "Heading"}, "Parent heading"
    )
    return Document(
        id="PARENT",
        metadata=Metadata(title="Parent doc"),
        parents=(),
        attributes=(),
        outline=(
            OutlineNode("PARENT-0", tuple(OutlineNode(o.id) for o in objects)),
        ),
        objects={o.id: o for o in (heading, *objects)},
        links={},
    )


def _req(obj_id: str, text: str) -> RequirementObject:
    return RequirementObject(obj_id, {"type": "Requirement"}, text)


def _trace_section(tex: str) -> str:
    return tex[tex.index(r"\syrssection{Traceability}"):]


def test_render_latex_traceability_link_table_and_uncovered(doc_folder: Path):
    doc = load_document(doc_folder)  # DOC-1 links to PARENT-42
    parent = _parent_doc(
        _req("PARENT-42", "Covered."), _req("PARENT-43", "Not covered.")
    )
    tex = render_latex(doc, parents={"PARENT": parent})
    # The section closes the document, after the last requirement card.
    trace = _trace_section(tex)
    assert tex.index(r"\begin{requirement}{DOC-1}") < tex.index(trace)
    assert trace.rstrip().endswith(r"\end{document}")
    assert r"\syrssubsection{Links to PARENT}" in trace
    # A link table: the requirement's id and its parent. No link state, no
    # row for headings, and no object text repeated here.
    assert r"\syrsmono{DOC-1} & \syrsmono{PARENT-42} \\" in trace
    assert "DOC-0" not in trace and "Top heading" not in trace
    assert "The system shall do X." not in trace
    assert "OK" not in trace and "SUSPECT" not in trace
    # Only the unlinked parent requirement is uncovered; headings never are.
    uncovered = trace[trace.index(r"\syrssubsection{Uncovered objects in PARENT}"):]
    assert r"\syrsmono{PARENT-43}" in uncovered
    assert "Not covered." not in uncovered
    assert "PARENT-42" not in uncovered
    assert "PARENT-0" not in uncovered
    assert "1 of 1 objects link to a parent document." in trace
    assert "PARENT: 1 of 2 objects have a link from this document." in trace


def test_render_latex_traceability_needs_parents(doc_folder: Path):
    """No ``parents`` passed, or a document without declared parents: the
    PDF has no Traceability section."""
    doc = load_document(doc_folder)
    assert r"\syrssection{Traceability}" not in render_latex(doc)
    orphan = dataclasses.replace(doc, parents=())
    assert r"\syrssection{Traceability}" not in render_latex(orphan, parents={})


def test_render_latex_traceability_untraced_and_non_parent_links(
    doc_folder: Path,
):
    """A link to a document that isn't a declared parent doesn't count."""
    (doc_folder / "links" / "DOC-1.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<links source="DOC-1">\n'
        '  <link target-doc="OTHER" target-id="OTHER-1" type="refines"/>\n'
        "</links>\n",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    trace = _trace_section(render_latex(
        doc, parents={"PARENT": _parent_doc(_req("PARENT-42", "Covered."))}
    ))
    assert r"\syrsmono{DOC-1} & \textcolor{inkFour}{none} \\" in trace
    assert "OTHER-1" not in trace
    assert "0 of 1 objects link to a parent document." in trace
    assert r"\syrsmono{PARENT-42}" in trace  # now uncovered


def test_render_latex_traceability_all_covered_and_missing_parent(
    doc_folder: Path,
):
    doc = load_document(doc_folder)
    covered = render_latex(
        doc, parents={"PARENT": _parent_doc(_req("PARENT-42", "Covered."))}
    )
    assert "Every object in PARENT has a link from this document." in covered
    # A declared parent that isn't loaded is named, not dropped.
    missing = render_latex(doc, parents={})
    assert "PARENT could not be loaded" in missing
    assert r"\syrsmono{DOC-1} & \syrsmono{PARENT-42} \\" in missing


def test_render_latex_traceability_escapes_ids(doc_folder: Path):
    """Ids reach LaTeX escaped, like the rest of the document."""
    doc = load_document(doc_folder)
    doc = dataclasses.replace(
        doc,
        objects={**doc.objects, "DOC_1": _req("DOC_1", "x")},
        outline=doc.outline + (OutlineNode("DOC_1"),),
    )
    trace = _trace_section(render_latex(
        doc, parents={"PARENT": _parent_doc(_req("PARENT_9", "x"))}
    ))
    assert r"\syrsmono{DOC\_1} & \textcolor{inkFour}{none}" in trace
    assert r"\syrsmono{PARENT\_9}" in trace


def test_pdf_passes_parents_through(doc_folder: Path, monkeypatch):
    seen = _capture_build(monkeypatch)
    pdf(
        load_document(doc_folder),
        parents={"PARENT": _parent_doc(_req("PARENT-42", "a"))},
    )
    assert r"\syrssection{Traceability}" in seen["tex"]


@_needs_tectonic
def test_pdf_compiles_with_traceability_section(doc_folder: Path):
    blob = pdf(
        load_document(doc_folder),
        rev="1",
        parents={"PARENT": _parent_doc(_req("PARENT-42", "a"), _req("PARENT-43", "b"))},
    )
    assert blob.startswith(b"%PDF")


# MELLON-PDF-SRS blind tests
import collections  # noqa: E402

from tests.pdf_support import (  # noqa: E402
    FIGURES_WRITE_EMPTY_FILES,
    PDF_PROGRAM,
    Pdf,
    _attrs_repo,
    _body,
    _broken_parent_repo,
    _check_default_attributes,
    _check_names_unloaded_parent,
    _figure_repo,
    _object_text,
    git_commit_all,
    heading,
    horizontal_rules,
    id_positions,
    make_program,
    mcp_calls,
    mcp_export,
    needs_pdf,
    obj,
    short,
    squash,
    strip_page_numbers,
    write_doc,
)


A4_W, A4_H = 595.28, 841.89


def header_texts(pdf: Pdf, index: int) -> list[str]:
    """The text in the header of page `index`: above the topmost long rule in
    the top quarter of the page, or else within 30 pt of the topmost text."""
    frags = pdf.fragments(index)
    if not frags:
        return []
    height = float(pdf.pages[index].mediabox.height)
    rules = [r for r in horizontal_rules(pdf, index) if r[2] > 0.75 * height]
    limit = max(r[2] for r in rules) if rules else max(y for _, _, y in frags) - 30
    return [t.strip() for t, _, y in frags if y > limit]


def words(text: str) -> collections.Counter:
    return collections.Counter(text.split())


@needs_pdf
def test_pdf_srs_73_placeholder_when_the_figure_program_writes_an_empty_file(tmp_path):
    """MELLON-PDF-SRS-73 and -75 (test case of MELLON-PDF-SRS-58): a figure the
    figure program can't draw (it writes an empty figure PDF) is shown as a
    placeholder that names the figure's file and says it isn't drawn."""
    root = _figure_repo(tmp_path / "repo")
    env = {"MELLON_DRAWIO": str(make_program(tmp_path / "fig", "drawio", FIGURES_WRITE_EMPTY_FILES))}
    out = tmp_path / "out.pdf"
    res = mcp_export(root, "DOC", out, env=env)
    assert res["ok"], res
    text = Pdf(out).flat()
    match = re.search(r"DOC-1\.svg[^\]\n]{0,60}", text)
    assert match, "no placeholder names the figure's file"
    assert re.search(r"not\s+(rendered|drawn)", match.group(0), re.I), match.group(0)


# ==========================================================================
# Cover page
# ==========================================================================

COVER = dict(title="Kappa Cover Heading", author="Quinn Rhodes", date="2026-05-17")


COVER_REV = "R5.4"


@pytest.fixture(scope="module")
def cover_pdfs(tmp_path_factory):
    """One document exported with each cover field set, then with each of
    title, author, date and revision empty in turn: {case: page 1 text}."""
    if PDF_PROGRAM is None:
        pytest.skip("the PDF program (tectonic) wasn't found: set MELLON_TECTONIC or put tectonic on PATH")
    root = tmp_path_factory.mktemp("cover") / "repo"
    objs = [obj("DOC-1", "Body text of the document.")]
    write_doc(root, "DOC", objs, **COVER)
    git_commit_all(root, "doc")
    cases = {
        "full": (COVER, COVER_REV),
        "title": ({**COVER, "title": ""}, COVER_REV),
        "author": ({**COVER, "author": ""}, COVER_REV),
        "date": ({**COVER, "date": ""}, COVER_REV),
        "revision": (COVER, None),
    }
    calls = []
    for case, (meta, rev) in cases.items():
        calls.append(lambda meta=meta: write_doc(root, "DOC", objs, **meta))
        args = {"doc_id": "DOC", "out_path": str(root.parent / f"{case}.pdf")}
        if rev is not None:
            args["rev"] = rev
        calls.append(("export_document", args))
    results = mcp_calls(root, calls)
    assert all(r["ok"] for r in results), results
    return {case: Pdf(root.parent / f"{case}.pdf").texts[0] for case in cases}


@needs_pdf
def test_pdf_srs_80_cover_shows_id_title_author_and_date(cover_pdfs):
    """MELLON-PDF-SRS-80: the first page is a cover page showing the document
    ID and the document's title, author and date."""
    page1 = cover_pdfs["full"]
    assert re.search(r"\bDOC\b(?!-)", page1), "no document ID on page 1"
    for field in ("title", "author", "date"):
        assert squash(COVER[field]) in squash(page1), f"page 1 doesn't show the {field}"


def _field_left_out(cover_pdfs, case: str, value: str, *, labelled: bool) -> None:
    full, empty = cover_pdfs["full"], cover_pdfs[case]
    added = words(empty) - words(full)
    assert not added, f"text shown only when the {case} is empty: {sorted(added)}"
    assert squash(value) not in squash(empty)
    if labelled:
        lines = [l.strip() for l in full.splitlines() if l.strip()]
        labels = [lines[i - 1] for i, l in enumerate(lines) if i and squash(l) == squash(value)]
        assert labels, "precondition: the field has a label line on the full cover"
        removed = words(full) - words(empty)
        for label in labels:
            for word in label.split():
                assert removed[word] >= 1, f"the {case} field's label {label!r} is still shown"


@needs_pdf
def test_pdf_srs_82_empty_title_left_out(cover_pdfs):
    """MELLON-PDF-SRS-82: when the title is empty, the cover leaves the field
    out, with no placeholder text: nothing takes the title's place."""
    _field_left_out(cover_pdfs, "title", COVER["title"], labelled=False)


@needs_pdf
def test_pdf_srs_82_empty_author_left_out(cover_pdfs):
    """MELLON-PDF-SRS-82: when the author is empty, the cover leaves out the
    author field, label and value, with no placeholder text."""
    _field_left_out(cover_pdfs, "author", COVER["author"], labelled=True)


@needs_pdf
def test_pdf_srs_82_empty_date_left_out(cover_pdfs):
    """MELLON-PDF-SRS-82: when the date is empty, the cover leaves out the date
    field, label and value, with no placeholder text."""
    _field_left_out(cover_pdfs, "date", COVER["date"], labelled=True)


@needs_pdf
def test_pdf_srs_82_empty_revision_left_out(cover_pdfs):
    """MELLON-PDF-SRS-82: when the revision to show is empty (no revision label,
    no issue), the cover leaves out the revision field, with no placeholder text."""
    _field_left_out(cover_pdfs, "revision", COVER_REV, labelled=True)


@pytest.fixture(scope="module")
def issue_cover(issue_repo, tmp_path_factory):
    if PDF_PROGRAM is None:
        pytest.skip("the PDF program (tectonic) wasn't found: set MELLON_TECTONIC or put tectonic on PATH")
    out = tmp_path_factory.mktemp("issue-out") / "out.pdf"
    res = mcp_export(issue_repo, "DOC", out)
    assert res["ok"], res
    return Pdf(out).texts[0]


@needs_pdf
def test_pdf_srs_84_export_tool_without_label_shows_current_issue(issue_cover):
    """MELLON-PDF-SRS-84: with no revision label, the cover shows the
    document's current issue as the revision."""
    assert "4.2.10" in squash(issue_cover), f"current issue 4.2.10 not on the cover: {issue_cover!r}"


@needs_pdf
def test_pdf_srs_84_current_issue_is_the_highest_in_issue_order(issue_cover):
    """MELLON-PDF-SRS-84, boundary of current issue: 4.2.10 comes after 4.2.9 in
    issue order, so the cover doesn't show 4.2.9 as the revision."""
    assert not re.search(r"4\.2\.9(?![0-9])", issue_cover), issue_cover


@needs_pdf
def test_pdf_srs_84_unreachable_issue_isnt_the_current_issue(issue_cover):
    """MELLON-PDF-SRS-84, boundary of current issue: an issue tag on a commit
    the latest commit doesn't reach isn't shown."""
    assert "9.9.9" not in issue_cover, issue_cover


@needs_pdf
def test_pdf_srs_84_export_tool_shows_the_revision_label(issue_repo, tmp_path):
    """MELLON-PDF-SRS-84: given the revision label X.1, the cover shows X.1 as
    the revision."""
    out = tmp_path / "out.pdf"
    res = mcp_export(issue_repo, "DOC", out, rev="X.1")
    assert res["ok"], res
    assert "X.1" in squash(Pdf(out).texts[0])


# ==========================================================================
# Pages, order, headings, objects, Traceability: one structured document
# ==========================================================================

STRUCT_REV = "R7.3"


STRUCT_LONG = " ".join(f"pad{i}" for i in range(1, 1300))


STRUCT_MENTIONS = 40


STRUCT_SECTIONS = [
    ("1", "Alpha Section"),
    ("1.1", "Alpha Sub One"),
    ("1.2", "Alpha Sub Second"),
    ("2", "Beta Section"),
    ("2.1", "Beta Sub One"),
    ("2.2", "Beta Sub Second"),
    ("3", "Traceability"),
]


STRUCT_REQS = ["DOC-30", "DOC-11", "DOC-14", "DOC-5", "DOC-23", "DOC-40"]  # outline order


STRUCT_HEADINGS = ["DOC-10", "DOC-2", "DOC-12", "DOC-13", "DOC-1", "DOC-20", "DOC-22"]


@pytest.fixture(scope="module")
def struct_pdf(tmp_path_factory):
    """DOC: two level 1 headings, each with two level 2 headings, a level 3
    heading, requirements whose IDs aren't in outline order, a parent PAR whose
    outline isn't in ID order; exported with revision label R7.3."""
    if PDF_PROGRAM is None:
        pytest.skip("the PDF program (tectonic) wasn't found: set MELLON_TECTONIC or put tectonic on PATH")
    root = tmp_path_factory.mktemp("struct") / "repo"
    write_doc(
        root,
        "PAR",
        [
            heading(
                "PAR-0",
                "Parent heading",
                *(obj(f"PAR-{n}", f"Parent {n} text.") for n in (6, 3, 1, 4, 2, 7)),
            )
        ],
    )
    ack = short(git_commit_all(root, "parent"))
    mentions = " ".join(f"see{i} DOC-11 and" for i in range(STRUCT_MENTIONS))
    write_doc(
        root,
        "DOC",
        [
            heading(
                "DOC-10",
                "Alpha Section",
                obj("DOC-30", "Alpha first requirement text.", links=[("PAR", "PAR-1", ack), ("PAR", "PAR-3", ack)]),
                heading("DOC-2", "Alpha Sub One", obj("DOC-11", "Alpha sub one requirement text.", links=[("PAR", "PAR-1", ack)])),
                heading("DOC-12", "Alpha Sub Second", heading("DOC-13", "Alpha Deep Third", obj("DOC-14", "Deep requirement text."))),
            ),
            heading(
                "DOC-1",
                "Beta Section",
                heading("DOC-20", "Beta Sub One", obj("DOC-5", STRUCT_LONG, links=[("PAR", "PAR-4", ack)])),
                heading(
                    "DOC-22",
                    "Beta Sub Second",
                    obj("DOC-23", "Beta sub second requirement text."),
                    obj("DOC-40", mentions),
                ),
            ),
        ],
        parents=("PAR",),
    )
    git_commit_all(root, "doc")
    out = root.parent / "struct.pdf"
    res = mcp_export(root, "DOC", out, rev=STRUCT_REV)
    assert res["ok"], res
    return Pdf(out)


def _section_line(num: str, title: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(num)}\.?{re.escape(squash(title))}$")


def _section_page(pdf: Pdf, num: str, title: str) -> int | None:
    """1-based number of the first page after the contents whose text has the
    section's title line."""
    pattern = _section_line(num, title)
    for index in range(2, len(pdf.texts)):
        if any(pattern.match(squash(line)) for line in pdf.texts[index].splitlines()):
            return index + 1
    return None


@needs_pdf
def test_pdf_srs_98_pages_are_a4_portrait(struct_pdf):
    """MELLON-PDF-SRS-98: each page of the exported PDF is A4 portrait,
    595.28 by 841.89 points."""
    assert len(struct_pdf.pages) >= 3
    for index, page in enumerate(struct_pdf.pages, 1):
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        if page.rotation % 180:
            width, height = height, width
        assert abs(width - A4_W) < 0.5 and abs(height - A4_H) < 0.5, f"page {index} is {width} x {height}"


@needs_pdf
def test_pdf_srs_99_contents_after_cover_lists_sections_with_pages(struct_pdf):
    """MELLON-PDF-SRS-99: after the cover page comes a table of contents that
    lists each numbered section of the top two levels, the Traceability section
    included, with the page the section starts on."""
    contents = [squash(line) for line in struct_pdf.texts[1].splitlines()]
    for num, title in STRUCT_SECTIONS:
        page = _section_page(struct_pdf, num, title)
        assert page is not None, f"precondition: section {num} {title} isn't in the body"
        entry = re.compile(rf"^{re.escape(num)}\.?{re.escape(squash(title))}\.*(\d+)$")
        found = [m for m in map(entry.match, contents) if m]
        assert found, f"the contents page doesn't list {num} {title}"
        assert int(found[0].group(1)) == page, f"{num} {title}: listed on page {found[0].group(1)}, starts on {page}"


def _bookmarks(pdf: Pdf):
    flat = []

    def walk(items):
        for item in items:
            if isinstance(item, list):
                walk(item)
            else:
                flat.append((item.title, pdf.reader.get_destination_page_number(item) + 1))

    walk(pdf.reader.outline)
    return flat


@needs_pdf
def test_pdf_srs_100_bookmark_for_each_contents_entry(struct_pdf):
    """MELLON-PDF-SRS-100: the exported PDF has a bookmark for each section the
    table of contents lists, opening the page the section starts on."""
    marks = [(re.sub(r"^[\d.]+", "", squash(t)), page) for t, page in _bookmarks(struct_pdf)]
    for num, title in STRUCT_SECTIONS:
        page = _section_page(struct_pdf, num, title)
        assert (squash(title), page) in marks, f"no bookmark for {num} {title} on page {page}: {marks}"


@needs_pdf
def test_pdf_srs_101_header_shows_id_title_revision_and_date(struct_pdf):
    """MELLON-PDF-SRS-101: each page after the cover shows in its header the
    document ID, the title, the revision the cover shows and the date."""
    assert STRUCT_REV in squash(struct_pdf.texts[0]), "precondition: the cover shows the revision"
    for index in range(1, len(struct_pdf.pages)):
        header = " ".join(header_texts(struct_pdf, index))
        assert re.search(r"\bDOC\b(?!-)", header), f"page {index + 1}: no document ID in {header!r}"
        assert squash("Blind Title").lower() in squash(header).lower(), f"page {index + 1}: no title in {header!r}"
        assert STRUCT_REV in squash(header), f"page {index + 1}: no revision in {header!r}"
        assert "2026-03-14" in header, f"page {index + 1}: no date in {header!r}"


@needs_pdf
def test_pdf_srs_105_search_finds_each_place_an_id_is_shown(struct_pdf):
    """MELLON-PDF-SRS-105: a search for an object ID finds each place the PDF
    shows it: DOC-11's own entry, its Traceability row, and the 40 places a
    description names it (some at a line end)."""
    text = " ".join(struct_pdf.texts)
    text = re.sub(r"\s+", " ", text)
    expected = STRUCT_MENTIONS + 2
    assert len(id_positions(text, "DOC-11")) == expected, (
        f"search finds DOC-11 {len(id_positions(text, 'DOC-11'))} times, the PDF shows it {expected} times"
    )


@needs_pdf
def test_pdf_srs_109_objects_in_outline_order(struct_pdf):
    """MELLON-PDF-SRS-109: the objects are shown in outline order, each object
    before its sub-objects, not in object ID order."""
    body = _body(struct_pdf)
    trace = body.find("Traceability")
    body = body[: trace if trace > 0 else len(body)]
    markers = [
        "AlphaSection",
        "DOC-30",
        "AlphaSubOne",
        "DOC-11",
        "AlphaSubSecond",
        "AlphaDeepThird",
        "DOC-14",
        "BetaSection",
        "BetaSubOne",
        "DOC-5",
        "BetaSubSecond",
        "DOC-23",
        "DOC-40",
    ]
    positions = []
    for m in markers:
        found = id_positions(body, m) if m.startswith("DOC-") else [body.find(m)]
        assert found and found[0] >= 0, f"{m} isn't shown"
        positions.append(found[0])
    assert positions == sorted(positions), [m for _, m in sorted(zip(positions, markers))]


GEN_WORDS = [
    "alder", "birch", "cedar", "hazel", "larch", "maple", "olive", "rowan", "sumac", "holly",
    "aspen", "elder", "pecan", "mango", "lemon", "guava", "acorn", "briar", "clove", "daisy",
]


def _generated_outline(seed: int):
    """A seeded random outline of headings (under headings only, up to level 3)
    and requirements (nested up to 2 deep), with shuffled object numbers.
    Returns (outline, markers in outline order, {heading title: number})."""
    import random

    rng = random.Random(seed)
    numbers = list(range(1, 201))
    rng.shuffle(numbers)
    headings = [0]
    words = iter(rng.sample(GEN_WORDS, len(GEN_WORDS)))
    markers: list[str] = []
    expected: dict[str, str] = {}

    def requirement(depth: int) -> dict:
        oid = f"DOC-{numbers.pop()}"
        markers.append(oid)
        kids = [requirement(depth + 1) for _ in range(rng.randint(0, 1))] if depth < 2 else []
        return obj(oid, f"Body text {len(markers)}.", children=tuple(kids))

    def section(level: int, number: str) -> dict:
        title = f"Part {next(words)}"
        oid = f"DOC-{numbers.pop()}"
        headings[0] += 1
        markers.append(squash(title))
        if level <= 2:
            expected[title] = number
        kids, sub = [], 0
        for _ in range(rng.randint(1, 3)):
            if level < 3 and rng.random() < 0.5 and headings[0] < 15:
                sub += 1
                kids.append(section(level + 1, f"{number}.{sub}"))
            else:
                kids.append(requirement(0))
        return heading(oid, title, *kids)

    outline = []
    top = 0
    for _ in range(3):
        if rng.random() < 0.25:
            outline.append(requirement(0))
        top += 1
        outline.append(section(1, str(top)))
    return outline, markers, expected


@pytest.fixture(scope="module", params=[11, 22, 33], ids=["seed11", "seed22", "seed33"])
def generated_pdf(request, tmp_path_factory):
    if PDF_PROGRAM is None:
        pytest.skip("the PDF program (tectonic) wasn't found: set MELLON_TECTONIC or put tectonic on PATH")
    outline, markers, expected = _generated_outline(request.param)
    root = tmp_path_factory.mktemp(f"generated{request.param}") / "repo"
    write_doc(root, "DOC", outline)
    git_commit_all(root, "doc")
    out = root.parent / "generated.pdf"
    res = mcp_export(root, "DOC", out)
    assert res["ok"], res
    return Pdf(out), markers, expected


@needs_pdf
def test_pdf_srs_109_generated_outlines_shown_in_outline_order(generated_pdf):
    """MELLON-PDF-SRS-109 (T11): for seeded random outlines, with object numbers
    shuffled, each object is shown in outline order, before its sub-objects."""
    pdf, markers, _ = generated_pdf
    body = _body(pdf)
    positions = []
    for m in markers:
        found = id_positions(body, m) if m.startswith("DOC-") else [body.find(m)]
        assert found and found[0] >= 0, f"{m} isn't shown"
        positions.append(found[0])
    assert positions == sorted(positions), [m for _, m in sorted(zip(positions, markers))]


@needs_pdf
def test_pdf_srs_114_generated_outlines_number_top_two_heading_levels(generated_pdf):
    """MELLON-PDF-SRS-114 (T11): for seeded random outlines, the headings of the
    top two outline levels are numbered in outline order, 1, 2 at the top level
    and k.1, k.2 under heading k; objects that aren't headings take no number."""
    pdf, markers, expected = generated_pdf
    for title, number in expected.items():
        assert _section_page(pdf, number, title), f"heading {title!r} isn't numbered {number}"
    numbered = re.compile(r"^\d+(\.\d+)*\.?\s")
    for line in (l.strip() for t in pdf.texts[2:] for l in t.splitlines()):
        for oid in (m for m in markers if m.startswith("DOC-")):
            if id_positions(line, oid):
                assert not numbered.match(line), f"{oid} is numbered: {line!r}"


@needs_pdf
def test_pdf_srs_114_top_two_heading_levels_numbered(struct_pdf):
    """MELLON-PDF-SRS-114: the headings of the top two outline levels are
    numbered in outline order 1, 1.1, 1.2, 2, 2.1, 2.2."""
    for num, title in STRUCT_SECTIONS[:-1]:
        assert _section_page(struct_pdf, num, title), f"no heading {num} {title}"


@needs_pdf
def test_pdf_srs_114_objects_other_than_headings_have_no_number(struct_pdf):
    """MELLON-PDF-SRS-114: an object that isn't a heading gets no number."""
    numbered = re.compile(r"^\d+(\.\d+)*\.?\s")
    starts = {"DOC-30": "Alpha first", "DOC-11": "Alpha sub one", "DOC-14": "Deep requirement", "DOC-23": "Beta sub second"}
    lines = [l.strip() for t in struct_pdf.texts[2:] for l in t.splitlines()]
    trace = next(i for i, l in enumerate(lines) if squash(l).endswith("Traceability"))
    for line in lines[:trace]:
        for oid, start in starts.items():
            if id_positions(line, oid) or start in line:
                assert not numbered.match(line), f"{oid} is numbered: {line!r}"


@needs_pdf
def test_pdf_srs_119_object_id_above_description(struct_pdf):
    """MELLON-PDF-SRS-119: an object that isn't a heading is shown with its ID
    above its description."""
    for oid, start in (("DOC-30", "Alpha first"), ("DOC-23", "Beta sub second")):
        index = struct_pdf.page_of(start, 2)
        assert index is not None
        frags = struct_pdf.fragments(index)
        id_y = [y for t, _, y in frags if id_positions(t, oid)]
        desc_y = [y for t, _, y in frags if start in t]
        assert id_y and desc_y, f"{oid}: ID or description not found on page {index + 1}"
        assert max(id_y) > max(desc_y), f"{oid}: the ID isn't above the description"


def pdf_trace_section(pdf: Pdf) -> str:
    text = squash(strip_page_numbers("\n".join(pdf.texts[2:])))
    starts = [m.end() for m in re.finditer(r"3\.?Traceability", text)]
    assert starts, "no Traceability section"
    return text[starts[0] :]


@needs_pdf
def test_pdf_srs_161_traceability_last_numbered_section_on_a_new_page(struct_pdf):
    """MELLON-PDF-SRS-161: a document with a parent ends with a numbered
    Traceability section that starts on a new page."""
    page = _section_page(struct_pdf, "3", "Traceability")
    assert page is not None, "no numbered Traceability section"
    index = page - 1
    frags = struct_pdf.fragments(index)
    title_y = max(y for t, _, y in frags if "Traceability" in squash(t))
    header = set(header_texts(struct_pdf, index))
    above = [t.strip() for t, _, y in frags if y > title_y + 1 and t.strip() not in header]
    assert not [t for t in above if not re.fullmatch(r"3\.?", t)], f"text above the section title: {above}"
    after = pdf_trace_section(struct_pdf)
    for _, title in STRUCT_SECTIONS[:-1]:
        assert squash(title) not in after, f"section {title} comes after Traceability"
    for start in ("Alphafirst", "Deeprequirement", "Betasubsecond", "pad1pad2"):
        assert start not in after, f"object text {start} comes after Traceability"


@needs_pdf
def test_pdf_srs_167_rows_in_outline_order_with_each_parent_id(struct_pdf):
    """MELLON-PDF-SRS-167: the Traceability section lists each object other than
    a heading, in outline order, with the IDs of the parent objects it links
    to (both of two), or 'none'."""
    section = pdf_trace_section(struct_pdf)
    rows = [id_positions(section, oid) for oid in STRUCT_REQS]
    assert all(rows), f"objects missing from the list: {[o for o, r in zip(STRUCT_REQS, rows) if not r]}"
    starts = [r[0] for r in rows]
    assert starts == sorted(starts), "rows aren't in outline order"
    for oid in STRUCT_HEADINGS:
        assert not id_positions(section, oid), f"heading {oid} is listed"
    row = dict(zip(STRUCT_REQS, [section[a:b] for a, b in zip(starts, starts[1:] + [len(section)])]))
    assert id_positions(row["DOC-30"], "PAR-1") and id_positions(row["DOC-30"], "PAR-3"), row["DOC-30"]
    assert "none" in row["DOC-14"].lower(), row["DOC-14"]
    assert "none" in row["DOC-23"].lower(), row["DOC-23"]


@needs_pdf
def test_pdf_srs_170_uncovered_parent_objects_in_parent_outline_order(struct_pdf):
    """MELLON-PDF-SRS-170: the uncovered objects of the parent are listed in the
    parent's outline order (PAR-6, PAR-2, PAR-7), not in ID order."""
    section = pdf_trace_section(struct_pdf)
    found = {oid: id_positions(section, oid) for oid in ("PAR-6", "PAR-2", "PAR-7")}
    assert all(len(p) == 1 for p in found.values()), found
    order = sorted(found, key=lambda oid: found[oid][0])
    assert order == ["PAR-6", "PAR-2", "PAR-7"], order


@pytest.fixture(scope="module")
def attrs_pdfs(tmp_path_factory):
    """The attributes document exported with no attribute list and with ['rationale']."""
    if PDF_PROGRAM is None:
        pytest.skip("the PDF program (tectonic) wasn't found: set MELLON_TECTONIC or put tectonic on PATH")
    root = _attrs_repo(tmp_path_factory.mktemp("attrs") / "repo")
    default, rationale = root.parent / "default.pdf", root.parent / "rationale.pdf"
    results = mcp_calls(
        root,
        [
            ("export_document", {"doc_id": "DOC", "out_path": str(default)}),
            ("export_document", {"doc_id": "DOC", "out_path": str(rationale), "attrs": ["rationale"]}),
        ],
    )
    assert all(r["ok"] for r in results), results
    return {"default": Pdf(default), "rationale": Pdf(rationale)}


@needs_pdf
def test_pdf_srs_125_export_tool_default_attributes(attrs_pdfs):
    """MELLON-PDF-SRS-125: with no attribute list, the export tool's PDF shows
    the attributes type, status, priority and verification-method, in that
    order, and no other (not rationale, not req-type)."""
    _check_default_attributes(attrs_pdfs["default"])


@needs_pdf
def test_pdf_srs_122_attribute_without_value_left_out(attrs_pdfs):
    """MELLON-PDF-SRS-122: below the description, the PDF shows each chosen
    attribute the object has a value for (type, status), and leaves out one it
    has no value for (priority, verification-method: no label, no empty value)."""
    pdf = attrs_pdfs["default"]
    part = _object_text(pdf, "DOC-2", None)
    desc = part.find("Secondobjectwords.")
    assert desc >= 0
    after = part[desc:]
    assert "Requirement" in after and "StatApproved" in after, f"type or status not shown below: {after!r}"
    assert "priority" not in part.lower(), "a priority label is shown for an object without a priority"
    assert "verification" not in part.lower(), "a verification-method label is shown without a value"
    index = pdf.page_of("Second object words.", 2)
    frags = pdf.fragments(index)
    desc_y = max(y for t, _, y in frags if "Second object" in t)
    status_y = [y for t, _, y in frags if "StatApproved" in t]
    assert status_y and max(status_y) < desc_y, "status isn't below the description"


@needs_pdf
def test_pdf_srs_126_export_tool_shows_only_the_listed_attributes(attrs_pdfs):
    """MELLON-PDF-SRS-126: given the list ['rationale'], the PDF shows rationale
    and none of the object's other attributes."""
    part = _object_text(attrs_pdfs["rationale"], "DOC-1", "DOC-2")
    assert "RatUnique" in part, "rationale isn't shown"
    for name, value in (("status", "StatDraft"), ("priority", "PrioHigh"), ("verification", "VmTest"), ("req-type", "RtFunctional")):
        assert value not in part and name not in part.lower(), f"{name} is shown"
    assert not re.search(r"type(?!-)", part.lower().replace("req-type", "")), "type is shown"


# ==========================================================================
# Text
# ==========================================================================

CHAR_CASES = {
    "abcd": "abcd",
    "efgh": "efgh",
    "ijkl": "ijkl",
    "mnop": "mnop",
    "qrst": "qrst",
    "uvwx": "uvwx",
    "yz": "yzyz",
    "ABCD": "ABCD",
    "EFGH": "EFGH",
    "IJKL": "IJKL",
    "MNOP": "MNOP",
    "QRST": "QRST",
    "UVWX": "UVWX",
    "YZ": "YZYZ",
    "digits-0123": "0123",
    "digits-4567": "4567",
    "digits-89": "8989",
    "underscore": "k_k",
    "less-than": "k<k",
    "greater-than": "k>k",
    "percent": "k%k",
    "ampersand": "k&k",
    "tilde": "k~k",
    "at": "k@k",
    "equals": "k=k",
    "open-paren": "k(k",
    "close-paren": "k)k",
    "slash": "k/k",
    "colon": "k:k",
    "semicolon": "k;k",
    "comma": "k,k",
    "full-stop": "k.k",
    "exclamation": "k!k",
    "single-hyphen": "k-k",
}


@pytest.fixture(scope="module")
def chars_text(tmp_path_factory):
    """Squashed text between the markers of a description holding each
    character of MELLON-PDF-SRS-135."""
    if PDF_PROGRAM is None:
        pytest.skip("the PDF program (tectonic) wasn't found: set MELLON_TECTONIC or put tectonic on PATH")
    root = tmp_path_factory.mktemp("chars") / "repo"
    desc = "BEGINMARK " + " ".join(CHAR_CASES.values()) + " ENDMARK"
    write_doc(root, "DOC", [obj("DOC-1", desc)])
    git_commit_all(root, "doc")
    out = root.parent / "chars.pdf"
    res = mcp_export(root, "DOC", out)
    assert res["ok"], res
    text = squash(Pdf(out).text)
    start, end = text.find("BEGINMARK"), text.find("ENDMARK")
    assert 0 <= start < end, "the description's markers aren't shown"
    return text[start + len("BEGINMARK") : end]


@needs_pdf
@pytest.mark.parametrize("case", list(CHAR_CASES), ids=list(CHAR_CASES))
def test_pdf_srs_135_character_shown_as_held(chars_text, case):
    """MELLON-PDF-SRS-135: in a description, the PDF shows each ASCII letter and
    digit, each of _ < > % & ~ @ = ( ) / : ; , . ! and a single hyphen as the
    document holds the character."""
    token = CHAR_CASES[case]
    assert token in chars_text, f"{token!r} isn't shown as held: {chars_text!r}"


@needs_pdf
def test_pdf_srs_135_description_shown_character_for_character(chars_text):
    """MELLON-PDF-SRS-135: the whole description, whitespace aside, is shown
    character for character as held."""
    assert chars_text == squash(" ".join(CHAR_CASES.values()))


@needs_pdf
@pytest.mark.parametrize("start", ["[", "*"], ids=["bracket", "star"])
def test_pdf_srs_135_letters_after_a_line_starting_with_bracket_or_star(tmp_path, start):
    """MELLON-PDF-SRS-135 (rationale CF-P24): a description line that starts with
    [ or * after a line break still shows its letters and digits as held."""
    root = tmp_path / "repo"
    write_doc(root, "DOC", [obj("DOC-1", f"First line here\n{start}Second line 42 here\nThird line")])
    git_commit_all(root, "doc")
    out = tmp_path / "out.pdf"
    res = mcp_export(root, "DOC", out)
    assert res["ok"], res
    text = squash(Pdf(out).text)
    for part in ("Firstlinehere", "Secondline42here", "Thirdline"):
        assert part in text, f"{part!r} isn't shown"


# ==========================================================================
# Tables
# ==========================================================================

TABLE_HEADER_ROWS = [(True, ["HdrOneA", "HdrOneB"]), (True, ["HdrSecA", "HdrSecB"])]


TABLE_BODY_ROWS = [(False, [f"B{i:03d}a", f"B{i:03d}b"]) for i in range(1, 81)]


SPLIT_BODY_ROWS = [(False, [f"S{i:03d}a", f"S{i:03d}b"]) for i in range(1, 81)]


@pytest.fixture(scope="module")
def tables_pdf(tmp_path_factory):
    if PDF_PROGRAM is None:
        pytest.skip("the PDF program (tectonic) wasn't found: set MELLON_TECTONIC or put tectonic on PATH")
    root = tmp_path_factory.mktemp("tables") / "repo"
    grid = [(False, [f"R{r}C{c}" for c in (1, 2, 3)]) for r in (1, 2, 3)]

    def one_column(align, digit):
        return {"cols": (align,), "rows": [(False, [digit]), (False, [digit * 8])]}

    write_doc(
        root,
        "DOC",
        [
            obj("DOC-1", "Grid caption", type="Table", table={"cols": ("left", "left", "left"), "rows": grid}),
            obj("DOC-2", "Left caption", type="Table", table=one_column("left", "1")),
            obj("DOC-3", "Centre caption", type="Table", table=one_column("center", "2")),
            obj("DOC-4", "Right caption", type="Table", table=one_column("right", "3")),
            obj("DOC-5", "Long caption", type="Table", table={"cols": ("left", "left"), "rows": TABLE_HEADER_ROWS + TABLE_BODY_ROWS}),
            obj(
                "DOC-6",
                "Split caption",
                type="Table",
                table={
                    "cols": ("left", "left"),
                    "rows": [(True, ["LeadA", "LeadB"]), (False, ["MidA", "MidB"]), (True, ["LaterA", "LaterB"])] + SPLIT_BODY_ROWS,
                },
            ),
        ],
    )
    git_commit_all(root, "doc")
    out = root.parent / "tables.pdf"
    res = mcp_export(root, "DOC", out)
    assert res["ok"], res
    return Pdf(out)


@needs_pdf
def test_pdf_srs_141_table_rows_and_cells_in_table_order(tables_pdf):
    """MELLON-PDF-SRS-141: an object's table is shown with each of its rows and
    cells, in the table's order (row by row)."""
    body = _body(tables_pdf)
    cells = [f"R{r}C{c}" for r in (1, 2, 3) for c in (1, 2, 3)]
    positions = [body.find(c) for c in cells]
    assert all(p >= 0 for p in positions), [c for c, p in zip(cells, positions) if p < 0]
    assert positions == sorted(positions), "the cells aren't in the table's order"


def _column_offset(pdf: Pdf, digit: str) -> float:
    """x of the one-digit cell minus x of the eight-digit cell of a one-column table."""
    for index in range(2, len(pdf.pages)):
        frags = pdf.fragments(index)
        narrow = [x for t, x, _ in frags if t.strip() == digit]
        wide = [x for t, x, _ in frags if t.strip() == digit * 8]
        if narrow and wide:
            return narrow[0] - wide[0]
    raise AssertionError(f"the {digit} table isn't shown")


@needs_pdf
def test_pdf_srs_146_cells_aligned_as_the_column_says(tables_pdf):
    """MELLON-PDF-SRS-146: cells are aligned as their column's alignment says.
    In one-column tables of a 1-digit and an 8-digit cell, the narrow cell starts
    where the wide one does (left), 3.5 digits in (centre), 7 digits in (right)."""
    left, centre, right = (_column_offset(tables_pdf, d) for d in "123")
    assert abs(left) < 0.5, f"left column: narrow cell starts {left:.1f} pt from the wide one"
    assert right > 5, f"right column: narrow cell starts {right:.1f} pt from the wide one"
    assert abs(centre / right - 0.5) < 0.1, f"centre column offset {centre:.1f} pt, right {right:.1f} pt"


def _continuation_pages(pdf: Pdf, body_prefix: str):
    """For each page after the first that shows body rows starting with
    `body_prefix`: (page index, fragments)."""
    pages = []
    for index in range(2, len(pdf.pages)):
        frags = pdf.fragments(index)
        if any(re.match(rf"{body_prefix}\d{{3}}a", t.strip()) for t, _, _ in frags):
            pages.append((index, frags))
    return pages[1:]


@needs_pdf
def test_pdf_srs_147_leading_header_rows_repeat_on_each_page(tables_pdf):
    """MELLON-PDF-SRS-147: a table's leading header rows are repeated at the top
    of each page the table continues onto (2 header rows, 80 body rows)."""
    pages = _continuation_pages(tables_pdf, "B")
    assert pages, "precondition: the table continues onto another page"
    for index, frags in pages:
        first_body = max(y for t, _, y in frags if re.match(r"B\d{3}a", t.strip()))
        for header in ("HdrOneA", "HdrSecA"):
            ys = [y for t, _, y in frags if header in squash(t)]
            assert ys and max(ys) > first_body, f"page {index + 1}: header row {header} isn't above the rows"


@needs_pdf
def test_pdf_srs_147_leading_header_row_repeats_when_a_body_row_follows_it(tables_pdf):
    """MELLON-PDF-SRS-147: with rows header, body, header, then 80 body rows, the
    leading header row is repeated at the top of each page the table continues
    onto."""
    pages = _continuation_pages(tables_pdf, "S")
    assert pages, "precondition: the table continues onto another page"
    for index, frags in pages:
        first_body = max(y for t, _, y in frags if re.match(r"S\d{3}a", t.strip()))
        ys = [y for t, _, y in frags if "LeadA" in squash(t)]
        assert ys and max(ys) > first_body, f"page {index + 1}: the leading header row isn't repeated"


# ==========================================================================
# Traceability section
# ==========================================================================


@pytest.fixture(scope="module")
def two_parents_section(tmp_path_factory):
    """DOC with parents UPA (3 objects) and UPB (5 objects): DOC-1 -> UPA-1,
    DOC-2 -> UPB-1, DOC-3 -> UPA-2 and UPB-1, DOC-4 none, DOC-0 a heading."""
    if PDF_PROGRAM is None:
        pytest.skip("the PDF program (tectonic) wasn't found: set MELLON_TECTONIC or put tectonic on PATH")
    root = tmp_path_factory.mktemp("parents") / "repo"
    write_doc(root, "UPA", [obj(f"UPA-{n}", f"Upper a {n}.") for n in (1, 2, 3)])
    write_doc(root, "UPB", [obj(f"UPB-{n}", f"Upper b {n}.") for n in (1, 2, 3, 4, 5)])
    ack = short(git_commit_all(root, "parents"))
    write_doc(
        root,
        "DOC",
        [
            heading(
                "DOC-0",
                "Only Section",
                obj("DOC-1", "Links to a.", links=[("UPA", "UPA-1", ack)]),
                obj("DOC-2", "Links to b.", links=[("UPB", "UPB-1", ack)]),
                obj("DOC-3", "Links to both.", links=[("UPA", "UPA-2", ack), ("UPB", "UPB-1", ack)]),
                obj("DOC-4", "Links nowhere."),
            )
        ],
        parents=("UPA", "UPB"),
    )
    git_commit_all(root, "doc")
    out = root.parent / "parents.pdf"
    res = mcp_export(root, "DOC", out)
    assert res["ok"], res
    pdf = Pdf(out)
    lines = strip_page_numbers("\n".join(pdf.texts[2:])).splitlines()
    start = next(i for i, l in enumerate(lines) if squash(l).endswith("Traceability"))
    return lines[start:]


@needs_pdf
def test_pdf_srs_163_outgoing_coverage_counts_an_object_once(two_parents_section):
    """MELLON-PDF-SRS-163: the Traceability section states the direct outgoing
    coverage by the parents: 3 of the 4 counted objects (DOC-3, linked to both
    parents, counts once; the heading isn't counted)."""
    text = " ".join(two_parents_section)
    assert re.search(r"(?<![\d.])3 of 4(?![\d])", text), text
    assert not re.search(r"(?<![\d.])4 of [45](?![\d])", text), text


@needs_pdf
def test_pdf_srs_164_incoming_coverage_for_each_parent(two_parents_section):
    """MELLON-PDF-SRS-164: for each parent, the Traceability section states the
    parent's direct incoming coverage by the document: UPA 2 of 3, UPB 1 of 5."""
    for parent, count in (("UPA", "2 of 3"), ("UPB", "1 of 5")):
        lines = [l for l in two_parents_section if re.search(rf"\b{parent}\b(?!-)", l) and count in l]
        assert lines, f"no line states {parent}: {count}: {two_parents_section}"


@needs_pdf
def test_pdf_srs_172_export_tool_names_a_parent_that_didnt_load(tmp_path):
    """MELLON-PDF-SRS-172: if a parent document has a load error, the export
    succeeds and the Traceability section names the parent and states that it
    didn't load (export tool)."""
    root = _broken_parent_repo(tmp_path / "repo")
    out = tmp_path / "out.pdf"
    res = mcp_export(root, "DOC", out)
    assert res["ok"], res
    _check_names_unloaded_parent(Pdf(out))

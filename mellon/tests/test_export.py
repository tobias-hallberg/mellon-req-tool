"""Tests for src/mellon/export.py — LaTeX/PDF export."""

from __future__ import annotations

import dataclasses
import re
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

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
        # Header rows split by a body row.
        (
            TableRow((TableCell("a"), TableCell("b")), header=True),
            TableRow((TableCell("c"), TableCell("d"))),
            TableRow((TableCell("e"), TableCell("f")), header=True),
            TableRow((TableCell("g"), TableCell("h"))),
        ),
        # A header cell whose rowspan reaches into the body.
        (
            TableRow((TableCell("tall", rowspan=2), TableCell("b")), header=True),
            TableRow((TableCell("c"),)),
        ),
        # No header rows at all.
        (TableRow((TableCell("a"), TableCell("b"))),),
    ],
    ids=["all-header", "trailing", "split", "rowspan-into-body", "none"],
)
def test_render_table_latex_no_repeating_head(rows):
    t = Table(columns=(TableColumn(), TableColumn()), rows=rows)
    assert r"\endhead" not in render_table_latex(t)


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


def test_render_latex_subject_falls_back_to_doc_id(doc_folder: Path):
    _replace_in(doc_folder / "DOC.xml", "<title>Test doc</title>", "<title></title>")
    assert r"\setDocSubject{DOC}" in render_latex(load_document(doc_folder))


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

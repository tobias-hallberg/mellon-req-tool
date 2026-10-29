"""LaTeX/PDF export.

End-to-end pipeline: ``Document`` → Jinja2-rendered LaTeX → ``tectonic`` → PDF
bytes. The template is bundled in ``mellon/templates/syrs/`` (``main.tex.j2``
+ ``syrs.cls``); both are written into the temp dir before tectonic runs so
``\\documentclass{syrs}`` resolves locally.

Jinja2 uses custom delimiters so LaTeX's own ``{}`` and ``%`` cannot collide:
``((* ... *))`` for blocks, ``((( ... )))`` for variables, ``((# ... #))`` for
comments.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from importlib.resources import files as _resource_files
from pathlib import Path, PurePosixPath

import jinja2

from . import drawio
from .schema import Document, OutlineNode, RequirementObject, Table

_BLOCK_START, _BLOCK_END = "((* ", " *))"
_VAR_START, _VAR_END = "(((", ")))"
_COMMENT_START, _COMMENT_END = "((# ", " #))"

_DEFAULT_ATTRS: tuple[str, ...] = (
    "type",
    "status",
    "priority",
    "verification-method",
)

_LATEX_ESCAPE: dict[str, str] = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

# Single-pass translation: each char is replaced exactly once, so a
# backslash's replacement (which itself contains `{}`) doesn't get
# re-escaped on a later pass.
_LATEX_TRANSLATE = str.maketrans(_LATEX_ESCAPE)

_HEADING_BY_DEPTH: dict[int, str] = {
    1: r"\syrssection",
    2: r"\syrssubsection",
    3: r"\syrssubsubsection",
    4: r"\syrsparagraph",
}


def escape_latex(s: str) -> str:
    """Escape LaTeX-significant characters in arbitrary text."""
    return s.translate(_LATEX_TRANSLATE)


def _one_line(s: str) -> str:
    """Collapse all whitespace runs (newlines included) to single spaces.

    For text that LaTeX sets on one line — headings, table captions and
    cells, cover metadata. A blank line there is a ``\\par``, which breaks
    the non-``\\long`` macros it lands in (``\\addcontentsline``,
    ``\\textbf``).
    """
    return " ".join(s.split())


def _heading_macro(depth: int) -> str:
    """LaTeX sectioning macro for an outline node at ``depth`` (1-based)."""
    return _HEADING_BY_DEPTH.get(depth, r"\subparagraph")


_COL_ALIGN: dict[str, str] = {"left": "l", "center": "c", "right": "r"}


def render_table_latex(table: Table) -> str:
    """Render a ``Table`` to a LaTeX ``longtable`` with booktabs rules.

    Page-breaks across "very big" tables via ``longtable``; merged cells become
    ``\\multicolumn`` (colspan) and ``\\multirow`` (rowspan). Header rows are
    bolded and separated from the body by a single ``\\midrule``. When they
    are the table's leading rows and no rowspan reaches past them into the
    body, they repeat at the top of every page (``\\endhead``).

    Cell text is escaped here, so the returned string is inserted into the
    template verbatim (NOT through ``escape_latex`` again). A geometrically
    consistent table is assumed (the loader/editor guarantee it); spans are
    clamped defensively so a stray hand-edited value can't desync the columns.
    """
    ncols = len(table.columns)
    if ncols == 0:
        return ""
    aligns = [_COL_ALIGN.get(col.align, "l") for col in table.columns]

    last_header = -1
    for i, row in enumerate(table.rows):
        if row.header:
            last_header = i
    # Header rows that open the table, with at least one body row after them.
    lead = next(
        (i for i, row in enumerate(table.rows) if not row.header),
        len(table.rows),
    )
    repeat_header = 0 < lead < len(table.rows) and last_header == lead - 1

    body: list[str] = []
    # Rowspans still covering rows below: each {col, width, remaining}.
    active: list[dict] = []
    for r, row in enumerate(table.rows):
        entries: list[str] = []
        new_spans: list[dict] = []
        c = 0
        cell_iter = iter(row.cells)
        while c < ncols:
            covering = next((s for s in active if s["col"] == c), None)
            if covering is not None:
                # Column(s) owned by a \multirow opened above: emit a blank
                # placeholder of the same width to keep the column count.
                width = covering["width"]
                entries.append(
                    rf"\multicolumn{{{width}}}{{{aligns[c]}}}{{}}"
                    if width > 1
                    else ""
                )
                c += width
                continue
            try:
                cell = next(cell_iter)
            except StopIteration:
                break  # geometry guarantees this won't happen for valid tables
            cs = min(max(cell.colspan, 1), ncols - c)
            rs = max(cell.rowspan, 1)
            text = escape_latex(_one_line(cell.text))
            if row.header and text:
                text = rf"\textbf{{{text}}}"
            content = rf"\multirow{{{rs}}}{{*}}{{{text}}}" if rs > 1 else text
            if cs > 1:
                entries.append(rf"\multicolumn{{{cs}}}{{{aligns[c]}}}{{{content}}}")
            else:
                entries.append(content)
            if rs > 1:
                new_spans.append({"col": c, "width": cs, "remaining": rs - 1})
            c += cs
        line = " & ".join(entries)
        # The `\\` ending the previous row (and booktabs' `\toprule` /
        # `\midrule`) look past whitespace for an optional `[...]` or a `*`,
        # so a row whose first cell starts with either would be swallowed
        # as an argument. An empty group ends that lookahead.
        if line.lstrip().startswith(("[", "*")):
            line = "{}" + line
        body.append("  " + line + r" \\")
        for s in active:
            s["remaining"] -= 1
        active = [s for s in active if s["remaining"] > 0] + new_spans
        if r == last_header:
            body.append(r"\midrule")
            # A \multirow still open would be cut off from its rows below.
            if repeat_header and not active:
                body.append(r"\endhead")

    lines = [rf"\begin{{longtable}}{{{''.join(aligns)}}}", r"\toprule"]
    lines.extend(body)
    lines.append(r"\bottomrule")
    lines.append(r"\end{longtable}")
    return "\n".join(lines)


def _flatten_outline(
    nodes: tuple[OutlineNode, ...], depth: int = 1
) -> list[tuple[int, str]]:
    """Pre-order flatten the outline tree to ``(depth, ref_id)`` pairs."""
    out: list[tuple[int, str]] = []
    for node in nodes:
        out.append((depth, node.ref_id))
        out.extend(_flatten_outline(node.children, depth + 1))
    return out


def _figure_note(obj: RequirementObject) -> str:
    """Placeholder line printed where an attached figure couldn't be rendered.

    A visible marker beats silently dropping the figure. Empty when the
    object has no figure.
    """
    if not obj.figure_path:
        return ""
    name = PurePosixPath(obj.figure_path).name
    return f"[Figure {name} not rendered in PDF export]"


def unrendered_figures(
    doc: Document, figures: Mapping[str, str] | None = None
) -> list[str]:
    """Ids of exported objects whose attached figure the PDF leaves out.

    ``figures`` is the obj_id → rendered-file map handed to
    ``render_latex``; ``None`` means nothing was rendered. Outline order,
    without duplicates; objects outside the outline aren't exported, so
    they're not listed. Empty means nothing is missing — the GUI/MCP export
    can warn when it isn't.
    """
    rendered = figures or {}
    ids = [
        ref_id
        for _, ref_id in _flatten_outline(doc.outline)
        if (obj := doc.objects.get(ref_id)) is not None
        and _figure_note(obj)
        and ref_id not in rendered
    ]
    return list(dict.fromkeys(ids))


def _build_items(
    doc: Document,
    attrs: tuple[str, ...],
    figures: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    """Pre-flatten the outline into per-row template inputs.

    Each entry has:
        depth   — 1-based outline depth
        ref_id  — referenced object id
        obj     — the RequirementObject, or None if missing
        kv      — list of (key, value) pairs for the requested attrs that are
                  actually present and non-empty on the object
        figure_file — the rendered figure (a file next to the .tex), or ""
        figure_note — placeholder text for a figure that wasn't rendered, or ""
    """
    rendered = figures or {}
    items: list[dict[str, object]] = []
    for depth, ref_id in _flatten_outline(doc.outline):
        obj = doc.objects.get(ref_id)
        if obj is None:
            items.append({
                "depth": depth,
                "ref_id": ref_id,
                "obj": None,
                "kv": [],
                "figure_file": "",
                "figure_note": "",
            })
            continue
        kv = [(k, obj.attrs[k]) for k in attrs if obj.attrs.get(k)]
        figure_file = rendered.get(ref_id, "") if obj.figure_path else ""
        item: dict[str, object] = {
            "depth": depth,
            "ref_id": ref_id,
            "obj": obj,
            "kv": kv,
            "figure_file": figure_file,
            "figure_note": "" if figure_file else _figure_note(obj),
        }
        if obj.table is not None:
            item["table_latex"] = render_table_latex(obj.table)
        items.append(item)
    return items


def _is_heading(obj: RequirementObject) -> bool:
    return obj.attrs.get("type") == "Heading"


def _build_trace(
    doc: Document, parents: Mapping[str, Document]
) -> dict[str, object]:
    """Template inputs for the Traceability section.

    ``rows`` has one ``{"id", "targets"}`` per non-heading object in outline
    order, ``targets`` being the ids its links point at in declared parent
    documents. ``uncovered`` has one entry per declared parent: the ids of
    its non-heading objects that no object of ``doc`` links to (as
    ``coverage_report`` counts them), or ``missing`` when the parent isn't
    in ``parents``. ``summary`` is a few plain-text lines of counts.
    """
    rows: list[dict[str, object]] = []
    n_objects = n_traced = 0
    for _, ref_id in _flatten_outline(doc.outline):
        obj = doc.objects.get(ref_id)
        if obj is None or _is_heading(obj):
            continue
        targets = [
            link.target_id
            for link in doc.links.get(ref_id, ())
            if link.target_doc in doc.parents
        ]
        n_objects += 1
        n_traced += bool(targets)
        rows.append({"id": ref_id, "targets": targets})
    # Coverage counts every link into a parent, outlined source or not.
    linked = {
        (link.target_doc, link.target_id)
        for link_list in doc.links.values()
        for link in link_list
    }

    uncovered: list[dict[str, object]] = []
    summary = [f"{n_traced} of {n_objects} objects link to a parent document."]
    for pid in doc.parents:
        parent = parents.get(pid)
        if parent is None:
            uncovered.append({"doc_id": pid, "missing": True, "ids": []})
            continue
        candidates = [
            obj
            for ref_id in dict.fromkeys(
                ref_id for _, ref_id in _flatten_outline(parent.outline)
            )
            if (obj := parent.objects.get(ref_id)) is not None
            and not _is_heading(obj)
        ]
        missing = [obj for obj in candidates if (pid, obj.id) not in linked]
        uncovered.append({
            "doc_id": pid,
            "missing": False,
            "ids": [obj.id for obj in missing],
        })
        summary.append(
            f"{pid}: {len(candidates) - len(missing)} of {len(candidates)} "
            "objects have a link from this document."
        )
    return {
        "parent_ids": list(doc.parents),
        "rows": rows,
        "uncovered": uncovered,
        "summary": summary,
    }


_TEMPLATE_FILE = "main.tex.j2"
_CLASS_FILE = "syrs.cls"


def _read_bundled(name: str) -> str:
    """Read a bundled file from ``mellon/templates/syrs/`` as text.

    Anchors on the ``mellon`` package and navigates into ``templates/syrs``
    so the subdirectories don't need to be Python sub-packages.
    """
    return (
        _resource_files("mellon") / "templates" / "syrs" / name
    ).read_text(encoding="utf-8")


def _make_env() -> jinja2.Environment:
    env = jinja2.Environment(
        block_start_string=_BLOCK_START,
        block_end_string=_BLOCK_END,
        variable_start_string=_VAR_START,
        variable_end_string=_VAR_END,
        comment_start_string=_COMMENT_START,
        comment_end_string=_COMMENT_END,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        undefined=jinja2.StrictUndefined,
    )
    env.filters["escape_latex"] = escape_latex
    env.filters["one_line"] = _one_line
    return env


def render_latex(
    doc: Document,
    *,
    attrs: tuple[str, ...] = _DEFAULT_ATTRS,
    rev: str | None = None,
    figures: Mapping[str, str] | None = None,
    parents: Mapping[str, Document] | None = None,
) -> str:
    """Render ``doc`` to a complete LaTeX source string.

    ``attrs`` selects which RequirementObject attributes appear under each
    non-heading object. Defaults to a small DOORS-style subset.

    ``rev`` populates the cover page's revision label. When ``None`` (the
    default) no revision is shown; resolving the current issue from git is a
    caller concern.

    ``figures`` maps obj_id → the file name of its rendered figure (a PDF
    that ``build_pdf`` places next to the .tex). Those print as numbered
    figures; any other attached figure prints the placeholder line. The
    caption is the object's description, except on headings and tables,
    whose description already prints as the heading or the table caption.

    ``parents`` (parent doc id → its ``Document``) ends the document with a
    Traceability section when ``doc`` declares parents: a link table, one
    row per non-heading object with the parent objects it links to, then per
    parent the objects nothing links to. A declared parent missing from
    ``parents`` is named as not loaded. ``None`` leaves the section out.
    Link states aren't shown; callers check suspect links before exporting.
    """
    trace = (
        _build_trace(doc, parents)
        if parents is not None and doc.parents
        else None
    )
    env = _make_env()
    template = env.from_string(_read_bundled(_TEMPLATE_FILE))
    return template.render(
        doc_id=doc.id,
        subject=doc.metadata.title or doc.id,
        owner=doc.metadata.author,
        date=doc.metadata.date,
        rev=rev,
        items=_build_items(doc, attrs, figures),
        heading_macro=_heading_macro,
        trace=trace,
    )


class TectonicError(RuntimeError):
    """Raised when tectonic cannot compile the LaTeX source."""


def _tectonic_in(folder: Path) -> str | None:
    """``folder/tectonic[.exe]`` if it exists."""
    for name in ("tectonic.exe", "tectonic"):
        cand = folder / name
        if cand.is_file():
            return str(cand)
    return None


def _find_tectonic() -> str | None:
    """Locate the tectonic binary.

    Search order:
        1. ``MELLON_TECTONIC`` — explicit path to the binary. Returned as-is
           (a bad path fails loudly at run time rather than being skipped).
        2. Frozen build: ``<exe-dir>/tools/tectonic[.exe]``, the copy the
           installer ships, so it wins over whatever is on PATH.
        3. ``PATH`` (the normal install case)
        4. ``<project-root>/tools/tectonic[.exe]`` — a vendored binary sitting
           alongside the source tree, for contributors who don't want a
           system-wide install. Only meaningful when running from a source
           checkout; in an installed wheel the relative path points inside
           site-packages, so installed users need tectonic on PATH.
    """
    override = os.environ.get("MELLON_TECTONIC")
    if override:
        return override
    if getattr(sys, "frozen", False):
        frozen = _tectonic_in(Path(sys.executable).parent / "tools")
        if frozen is not None:
            return frozen
    on_path = shutil.which("tectonic")
    if on_path is not None:
        return on_path
    return _tectonic_in(Path(__file__).resolve().parents[2] / "tools")


# Wall clock limit. A first-ever export on a cold cache downloads the TeX
# support files it needs, so this is generous.
_TECTONIC_TIMEOUT_S = 300

# A pre-seeded TeX cache shipped next to the binary (the installer's
# `tools/tectonic-cache`). Used unless the caller set TECTONIC_CACHE_DIR.
_CACHE_DIR_NAME = "tectonic-cache"


def _run_tectonic(
    tectonic: str, args: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run ``tectonic *args`` without a console window or inherited stdin.

    A timeout or an unrunnable binary raises ``TectonicError``.
    """
    env = dict(os.environ)
    cache = Path(tectonic).parent / _CACHE_DIR_NAME
    if "TECTONIC_CACHE_DIR" not in env and cache.is_dir():
        env["TECTONIC_CACHE_DIR"] = str(cache)
    creationflags = (
        subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    )
    try:
        return subprocess.run(
            [tectonic, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            # Tectonic writes UTF-8; the locale codepage (cp1252 on Windows)
            # would crash on characters like "₁".
            encoding="utf-8",
            errors="replace",
            timeout=_TECTONIC_TIMEOUT_S,
            creationflags=creationflags,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise TectonicError(
            f"tectonic did not finish within {_TECTONIC_TIMEOUT_S} s and "
            "was stopped (a first export may be downloading TeX files — "
            "check the network and try again)"
        ) from e
    except OSError as e:
        raise TectonicError(f"could not run tectonic at {tectonic}: {e}") from e


def build_pdf(tex: str, assets: Mapping[str, Path] | None = None) -> bytes:
    """Compile a LaTeX source string to PDF bytes via tectonic.

    ``assets`` maps a file name the source refers to (a figure's
    ``\\includegraphics``) to the file copied next to the .tex first.

    Raises ``TectonicError`` if the binary is missing or compilation fails.
    """
    tectonic = _find_tectonic()
    if tectonic is None:
        raise TectonicError(
            "tectonic binary not found on PATH — install from "
            "https://github.com/tectonic-typesetting/tectonic/releases "
            "(source checkouts may also drop it in <project-root>/tools/, "
            "or set MELLON_TECTONIC to its path)"
        )
    with tempfile.TemporaryDirectory(prefix="mellon-export-") as tmp:
        tmp_path = Path(tmp)
        tex_path = tmp_path / "doc.tex"
        tex_path.write_text(tex, encoding="utf-8")
        # `\documentclass{syrs}` is resolved relative to the input file, so
        # the .cls must live next to doc.tex in the same temp dir.
        (tmp_path / _CLASS_FILE).write_text(
            _read_bundled(_CLASS_FILE), encoding="utf-8"
        )
        for name, src in (assets or {}).items():
            shutil.copyfile(src, tmp_path / name)
        result = _run_tectonic(
            tectonic,
            ["-X", "compile", "--outdir", str(tmp_path), str(tex_path)],
        )
        if result.returncode != 0:
            # Cold cache + no network: tectonic panics with this message.
            hint = (
                "The TeX support files aren't cached yet and couldn't be "
                "downloaded; the first PDF export needs internet access.\n"
                if "couldn't get it from the internet" in result.stderr
                else ""
            )
            raise TectonicError(
                f"{hint}tectonic exited {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        pdf_path = tmp_path / "doc.pdf"
        if not pdf_path.is_file():
            raise TectonicError(
                f"tectonic ran successfully but no PDF at {pdf_path}\n"
                f"stdout:\n{result.stdout}"
            )
        return pdf_path.read_bytes()


def _render_figures(
    doc: Document, folder: Path, out_dir: Path
) -> tuple[dict[str, str], dict[str, Path]]:
    """Render the exported objects' figures to PDF with draw.io.

    Returns ``(figures, assets)``: obj_id → file name for ``render_latex``,
    and file name → rendered PDF for ``build_pdf``. Both are empty when no
    exported object has a figure (draw.io isn't started), when draw.io isn't
    installed, or when it can't run. A figure draw.io couldn't render is left
    out, so it falls back to the placeholder line.
    """
    ids = list(dict.fromkeys(
        ref_id
        for _, ref_id in _flatten_outline(doc.outline)
        if (obj := doc.objects.get(ref_id)) is not None and obj.figure_path
    ))
    if not ids:
        return {}, {}
    exe = drawio.find_drawio()
    if exe is None:
        return {}, {}
    svgs = [folder / doc.objects[obj_id].figure_path for obj_id in ids]
    try:
        pdfs = drawio.export_pdfs(exe, svgs, out_dir)
    except drawio.DrawioError:
        return {}, {}
    figures: dict[str, str] = {}
    assets: dict[str, Path] = {}
    for n, obj_id in enumerate(ids):
        if n in pdfs:
            figures[obj_id] = pdfs[n].name
            assets[pdfs[n].name] = pdfs[n]
    return figures, assets


def pdf(
    doc: Document,
    *,
    folder: Path | None = None,
    attrs: tuple[str, ...] = _DEFAULT_ATTRS,
    rev: str | None = None,
    unrendered: list[str] | None = None,
    parents: Mapping[str, Document] | None = None,
) -> bytes:
    """Render ``doc`` to LaTeX, then compile to PDF bytes via tectonic.

    With ``folder`` (the document's folder on disk), attached figures are
    rendered through draw.io desktop and print as numbered figures. Without
    it, or for a figure draw.io can't render, the figure prints as a
    placeholder line. ``unrendered``, when given, is extended with the ids of
    the objects whose figure the PDF leaves out (see ``unrendered_figures``).
    ``parents`` adds the Traceability section (see ``render_latex``).
    """
    with tempfile.TemporaryDirectory(prefix="mellon-figures-") as tmp:
        if folder is not None:
            figures, assets = _render_figures(doc, Path(folder), Path(tmp))
        else:
            figures, assets = {}, {}
        if unrendered is not None:
            unrendered.extend(unrendered_figures(doc, figures))
        tex = render_latex(
            doc, attrs=attrs, rev=rev, figures=figures, parents=parents
        )
        return build_pdf(tex, assets)

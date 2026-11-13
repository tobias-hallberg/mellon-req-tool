"""Shared helpers for the MELLON-PDF-SRS blind tests (docs/specification.md, step 7).

Documents built on disk in the on-disk format of USAGE.md; stand-in PDF and figure
programs; the MCP export tool in process and over stdio; readers for the exported PDF
(text, words, positions, drawing, header and rules, through pypdf); and an offscreen
driver for the Document Editor's export, its warning, save dialog and notices. Tests
that need the PDF program (tectonic) or draw.io desktop skip when the program can't
be found. Fixtures defined here are imported by name into the test modules that use
them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import timedelta
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape, quoteattr as _xml_attr

import pygit2
import pytest
from pypdf import PdfReader

from mellon.drawio import find_drawio


# --------------------------------------------------------------------------
# The PDF program and the figure program
# --------------------------------------------------------------------------


def _find_pdf_program() -> str | None:
    """Where a PDF program can be found: MELLON_TECTONIC, PATH, the installed
    release's copy, a source checkout's tools folder."""
    env = os.environ.get("MELLON_TECTONIC")
    if env and Path(env).is_file():
        return env
    on_path = shutil.which("tectonic")
    if on_path:
        return on_path
    local = os.environ.get("LOCALAPPDATA")
    if local:
        installed = Path(local) / "Programs" / "Mellon" / "tools" / "tectonic.exe"
        if installed.is_file():
            return str(installed)
    here = Path(__file__).resolve()
    for tools in (here.parents[1] / "tools", here.parents[2] / "tools"):
        for name in ("tectonic.exe", "tectonic"):
            if (tools / name).is_file():
                return str(tools / name)
    return None


PDF_PROGRAM = _find_pdf_program()


needs_pdf = pytest.mark.skipif(
    PDF_PROGRAM is None,
    reason="the PDF program (tectonic) wasn't found: set MELLON_TECTONIC or put tectonic on PATH",
)


def make_program(folder: Path, name: str, body: str) -> Path:
    """A stand-in program `name` that runs the Python `body` with sys.argv.

    On Windows a .cmd launcher, elsewhere an executable shell script."""
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / f"{name}.py"
    script.write_text(body, encoding="utf-8", newline="\n")
    if os.name == "nt":
        launcher = folder / f"{name}.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8", newline="")
    else:
        launcher = folder / name
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8", newline="\n"
        )
        launcher.chmod(0o755)
    return launcher


# A stand-in PDF program: called as `-X compile --outdir <dir> <file>.tex`, it
# writes a one-page PDF named after the .tex file into <dir>.
STANDIN_PDF_PROGRAM = r'''
import sys
from pathlib import Path
from pypdf import PdfWriter
args = sys.argv[1:]
out_dir = Path(args[args.index("--outdir") + 1]) if "--outdir" in args else None
tex = next(Path(a) for a in args if a.endswith(".tex"))
w = PdfWriter()
w.add_blank_page(width=595.28, height=841.89)
with open((out_dir or tex.parent) / (tex.stem + ".pdf"), "wb") as f:
    w.write(f)
'''


# --------------------------------------------------------------------------
# Building documents on disk (USAGE.md's on-disk format)
# --------------------------------------------------------------------------

SCHEMA: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("type", "enum", ("Heading", "Requirement", "Information", "Table")),
    ("status", "enum", ("StatDraft", "StatApproved")),
    ("priority", "enum", ("PrioHigh", "PrioLow")),
    ("verification-method", "enum", ("VmTest", "VmAnalysis")),
    ("rationale", "text", ()),
    ("req-type", "enum", ("RtFunctional", "RtSafety")),
)


def obj(
    oid: str,
    desc: str = "",
    *,
    type: str = "Requirement",
    children: tuple = (),
    attrs: dict[str, str] | None = None,
    table: dict | None = None,
    figure: bytes | None = None,
    links: tuple = (),
) -> dict:
    """One object for `write_doc`. `links` holds (target_doc, target_id, ack) tuples."""
    return dict(
        id=oid,
        desc=desc,
        type=type,
        children=tuple(children),
        attrs=dict(attrs or {}),
        table=table,
        figure=figure,
        links=tuple(links),
    )


def heading(oid: str, title: str, *children) -> dict:
    return obj(oid, title, type="Heading", children=children)


def _table_xml(table: dict) -> str:
    """`table` = {"cols": ("left", ...), "rows": [(header, [cell, ...]), ...]};
    a cell is a text or (text, colspan, rowspan)."""
    out = ["  <table>", "    <columns>"]
    out += [f"      <col align={_xml_attr(a)}/>" for a in table["cols"]]
    out.append("    </columns>")
    for header, cells in table["rows"]:
        out.append('    <row header="true">' if header else "    <row>")
        for cell in cells:
            text, colspan, rowspan = (cell, 1, 1) if isinstance(cell, str) else cell
            span = ""
            if colspan > 1:
                span += f' colspan="{colspan}"'
            if rowspan > 1:
                span += f' rowspan="{rowspan}"'
            out.append(f"      <cell{span}>{_xml_escape(text)}</cell>")
        out.append("    </row>")
    out.append("  </table>")
    return "\n".join(out)


def _flatten(objs) -> list[dict]:
    flat = []
    for o in objs:
        flat.append(o)
        flat.extend(_flatten(o["children"]))
    return flat


def _outline_xml(objs, indent: int = 4) -> list[str]:
    lines = []
    pad = " " * indent
    for o in objs:
        if o["children"]:
            lines.append(f'{pad}<ref id="{o["id"]}">')
            lines += _outline_xml(o["children"], indent + 2)
            lines.append(f"{pad}</ref>")
        else:
            lines.append(f'{pad}<ref id="{o["id"]}"/>')
    return lines


def write_doc(
    root: Path,
    doc_id: str,
    objs,
    *,
    title: str | None = "Blind Title",
    author: str | None = "Blind Author",
    date: str | None = "2026-03-14",
    parents: tuple[str, ...] = (),
    schema=SCHEMA,
) -> Path:
    """Write a whole document folder: manifest, object, links and figure files."""
    folder = root / doc_id
    (folder / "objects").mkdir(parents=True, exist_ok=True)
    meta = []
    for tag, value in (("title", title), ("author", author), ("date", date)):
        if value is not None:
            meta.append(f"    <{tag}>{_xml_escape(value)}</{tag}>")
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<document id="{doc_id}">', "  <metadata>"]
    lines += meta
    lines.append("  </metadata>")
    if parents:
        lines.append("  <parents>")
        lines += [f'    <parent doc="{p}"/>' for p in parents]
        lines.append("  </parents>")
    lines.append("  <attributes>")
    for name, kind, options in schema:
        req = "true" if name == "type" else "false"
        if options:
            lines.append(f'    <attribute name="{name}" kind="{kind}" required="{req}">')
            lines += [f"      <option>{_xml_escape(o)}</option>" for o in options]
            lines.append("    </attribute>")
        else:
            lines.append(f'    <attribute name="{name}" kind="{kind}" required="{req}"/>')
    lines.append("  </attributes>")
    lines.append("  <outline>")
    lines += _outline_xml(objs)
    lines.append("  </outline>")
    lines.append("</document>")
    (folder / f"{doc_id}.xml").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    for o in _flatten(objs):
        write_object(folder, o)
    return folder


def write_object(folder: Path, o: dict) -> None:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<object id="{o["id"]}">']
    lines.append(f'  <attr name="type">{_xml_escape(o["type"])}</attr>')
    for name, value in o["attrs"].items():
        lines.append(f"  <attr name={_xml_attr(name)}>{_xml_escape(value)}</attr>")
    lines.append(f"  <description>{_xml_escape(o['desc'])}</description>")
    if o["table"]:
        lines.append(_table_xml(o["table"]))
    lines.append("</object>")
    (folder / "objects" / f"{o['id']}.xml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )
    if o["links"]:
        write_links(folder, o["id"], o["links"])
    if o["figure"] is not None:
        (folder / "figures").mkdir(exist_ok=True)
        (folder / "figures" / f"{o['id']}.svg").write_bytes(o["figure"])


def write_links(folder: Path, source_id: str, links) -> None:
    (folder / "links").mkdir(exist_ok=True)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<links source="{source_id}">']
    for target_doc, target_id, ack in links:
        ack_attr = f' ack="{ack}"' if ack else ""
        lines.append(
            f'  <link target-doc="{target_doc}" target-id="{target_id}" type="satisfies"{ack_attr}/>'
        )
    lines.append("</links>")
    (folder / "links" / f"{source_id}.xml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def plain_svg(width: int = 200, height: int = 100) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><rect x="0" y="0" width="{width}" height="{height}" '
        f'fill="#88aacc" stroke="#000000"/></svg>'
    ).encode()


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

SIG = pygit2.Signature("Blind Tester", "blind.tester@example.invalid")


def git_commit_all(root: Path, message: str) -> pygit2.Oid:
    """Stage every file under `root` (deletions too) and commit on HEAD."""
    try:
        repo = pygit2.Repository(str(root))
    except pygit2.GitError:
        repo = pygit2.init_repository(str(root))
    repo.index.add_all()
    for entry in list(repo.index):
        if not (root / entry.path).exists():
            repo.index.remove(entry.path)
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    return repo.create_commit("HEAD", SIG, SIG, message, tree, parents)


def short(oid: pygit2.Oid) -> str:
    return str(oid)[:7]


def tag_issue(root: Path, doc_id: str, issue: str, oid: pygit2.Oid) -> None:
    pygit2.Repository(str(root)).references.create(f"refs/tags/{doc_id}-issue-{issue}", oid)


# --------------------------------------------------------------------------
# The MCP server over stdio
# --------------------------------------------------------------------------

_SERVER = "import sys; from mellon.mcp.server import main; sys.exit(main())"


def _tool_result(res) -> object:
    sc = res.structuredContent
    if sc is not None:
        return sc["result"] if set(sc) == {"result"} else sc
    texts = [c.text for c in res.content if getattr(c, "text", None) is not None]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except ValueError:
            return texts[0]
    return [json.loads(t) for t in texts]


def mcp_calls(root: Path, calls, *, env: dict[str, str] | None = None, cwd: Path | None = None):
    """Start `mellon-mcp --repo-root root`, make each tool call in `calls` and
    return their results. An item of `calls` may be a plain callable, which is
    run between two tool calls."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    full_env = dict(os.environ)
    full_env.pop("MELLON_DRAWIO", None)
    if PDF_PROGRAM:
        full_env["MELLON_TECTONIC"] = PDF_PROGRAM
    full_env.update(env or {})
    for key in [k for k, v in full_env.items() if v is None]:
        del full_env[key]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", _SERVER, "--repo-root", str(root)],
        env=full_env,
        cwd=str(cwd or root),
    )

    async def run():
        results = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=600)) as s:
                await s.initialize()
                for call in calls:
                    if callable(call):
                        call()
                        continue
                    name, args = call
                    results.append(_tool_result(await s.call_tool(name, args)))
        return results

    return asyncio.run(run())


def mcp_export(root: Path, doc_id: str, out: Path, **kwargs) -> dict:
    env = kwargs.pop("env", None)
    args = {"doc_id": doc_id, "out_path": str(out), **kwargs}
    return mcp_calls(root, [("export_document", args)], env=env)[0]


# --------------------------------------------------------------------------
# Reading the exported PDF
# --------------------------------------------------------------------------


class Pdf:
    """An exported PDF, read with pypdf."""

    def __init__(self, path: Path):
        self.path = path
        self.reader = PdfReader(str(path))
        self.pages = self.reader.pages
        self.texts = [p.extract_text() or "" for p in self.pages]

    @property
    def text(self) -> str:
        return "\n".join(self.texts)

    def flat(self, start: int = 0) -> str:
        """The text of pages start.. with each run of whitespace made one space."""
        return re.sub(r"\s+", " ", "\n".join(self.texts[start:]))

    def fragments(self, index: int) -> list[tuple[str, float, float]]:
        """(text, x, y) of each piece of text that page `index` shows."""
        out: list[tuple[str, float, float]] = []

        def visit(text, cm, tm, font_dict, font_size):
            if text and text.strip():
                x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
                y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
                out.append((text, x, y))

        self.pages[index].extract_text(visitor_text=visit)
        return out

    def page_of(self, needle: str, start: int = 0) -> int | None:
        """Index of the first page from `start` whose text holds `needle`."""
        for i in range(start, len(self.texts)):
            if needle in re.sub(r"\s+", " ", self.texts[i]):
                return i
        return None


def strip_page_numbers(text: str) -> str:
    """Text with each "Page k of N" taken out, so that it can't pass for a count."""
    return re.sub(r"Page\s+\d+\s+of\s+\d+", " ", text, flags=re.I)


def squash(text: str) -> str:
    """`text` without whitespace: PDF text readers split and join words at
    kerning and line ends, which says nothing about the characters shown."""
    return re.sub(r"\s+", "", text)


def id_positions(text: str, oid: str) -> list[int]:
    """Where object ID `oid` stands in `text`, not as the start of a longer ID."""
    return [m.start() for m in re.finditer(re.escape(oid) + r"(?![0-9])", text)]


def _mul(a, b):
    """Matrix product a x b, PDF row-vector convention."""
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    )


def _apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def drawing(pdf: Pdf, index: int):
    """What page `index` draws, in page coordinates: straight lines as
    ((x1, y1), (x2, y2)) and placed forms (figures) as dicts with the placed
    box (x0, y0, x1, y1) and the form's own BBox."""
    from pypdf.generic import ContentStream

    page = pdf.pages[index]
    ops = ContentStream(page.get_contents(), pdf.reader).operations
    resources = page.get("/Resources") or {}
    xobjects = resources.get("/XObject") or {}
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    stack: list = []
    current = None
    lines, forms = [], []
    for operands, op in ops:
        if op == b"q":
            stack.append(ctm)
        elif op == b"Q":
            ctm = stack.pop() if stack else ctm
        elif op == b"cm":
            ctm = _mul(tuple(float(v) for v in operands), ctm)
        elif op == b"m":
            current = _apply(ctm, float(operands[0]), float(operands[1]))
        elif op == b"l" and current is not None:
            end = _apply(ctm, float(operands[0]), float(operands[1]))
            lines.append((current, end))
            current = end
        elif op == b"re":
            x, y, w, h = (float(v) for v in operands)
            if abs(h) < 2:  # a filled thin rectangle is a rule
                lines.append((_apply(ctm, x, y), _apply(ctm, x + w, y)))
        elif op == b"Do":
            xo = xobjects[operands[0]].get_object()
            if xo.get("/Subtype") != "/Form":
                continue
            bbox = [float(v) for v in xo["/BBox"]]
            matrix = tuple(float(v) for v in xo.get("/Matrix", [1, 0, 0, 1, 0, 0]))
            m = _mul(matrix, ctm)
            corners = [_apply(m, x, y) for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])]
            xs, ys = [c[0] for c in corners], [c[1] for c in corners]
            forms.append(dict(name=str(operands[0]), box=(min(xs), min(ys), max(xs), max(ys)), bbox=bbox))
    return lines, forms


def horizontal_rules(pdf: Pdf, index: int, min_length: float = 150.0):
    """(x_left, x_right, y) of each horizontal line at least min_length long."""
    lines, _ = drawing(pdf, index)
    rules = []
    for (x1, y1), (x2, y2) in lines:
        if abs(y1 - y2) < 0.5 and abs(x2 - x1) >= min_length:
            rules.append((min(x1, x2), max(x1, x2), y1))
    return rules


# --------------------------------------------------------------------------
# The Document Editor, offscreen
# --------------------------------------------------------------------------


class Gui:
    """Drives Document Editor windows. Each dialog that would wait for input is
    patched: the export warning (a message box) is answered as `warning_choice`
    says, the save dialog returns `save_path`, and each other message box or
    dialog is recorded in `events`."""

    def __init__(self, qtbot, monkeypatch):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

        self.qtbot = qtbot
        self.events: list[dict] = []
        self.warning_choice = "export"  # "export", "cancel" or "enter"
        self.save_path: str | None = None
        gui = self

        def any_exec(dialog):
            if not isinstance(dialog, QMessageBox):
                gui.events.append(dict(kind="dialog", cls=type(dialog).__name__))
                return 0
            default = dialog.defaultButton()
            gui.events.append(
                dict(
                    kind="box",
                    text=dialog.text(),
                    info=dialog.informativeText(),
                    detail=dialog.detailedText(),
                    buttons=[b.text().replace("&", "") for b in dialog.buttons()],
                    default=default.text().replace("&", "") if default is not None else None,
                )
            )
            if gui.warning_choice == "enter":
                dialog.show()
                QTest.keyClick(dialog, Qt.Key.Key_Return)
                return dialog.result()
            wanted = "Export anyway" if gui.warning_choice == "export" else "Cancel"
            for button in dialog.buttons():
                if button.text().replace("&", "") == wanted:
                    button.click()
                    return dialog.result()
            return 0

        def save_dialog(*args, **kwargs):
            gui.events.append(dict(kind="save", args=[a for a in args if isinstance(a, str)]))
            return (gui.save_path or "", "PDF (*.pdf)")

        def notice(kind):
            def show(*args, **kwargs):
                texts = [a for a in args if isinstance(a, str)]
                gui.events.append(
                    dict(kind=kind, title=texts[0] if texts else "", text="\n".join(texts[1:]))
                )
                return QMessageBox.StandardButton.Ok

            return staticmethod(show)

        monkeypatch.setattr(QMessageBox, "exec", any_exec)
        monkeypatch.setattr(QDialog, "exec", any_exec)
        monkeypatch.setattr(QFileDialog, "exec", any_exec)
        monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(save_dialog))
        for kind in ("information", "warning", "critical", "question"):
            monkeypatch.setattr(QMessageBox, kind, notice(kind))

    def open(self, root: Path, doc_id: str, service=None):
        from mellon.gui.main_window import DocumentEditorWindow
        from mellon.service.document import DocumentService

        win = DocumentEditorWindow(doc_id, service or DocumentService(root, cache=True))
        self.qtbot.addWidget(win)
        win.show()
        self.qtbot.wait(200)
        return win

    @staticmethod
    def export_action(win):
        from PySide6.QtGui import QAction

        return next(a for a in win.findChildren(QAction) if a.text().replace("&", "") == "Export")

    def wait_idle(self, win, timeout: int = 240_000) -> None:
        self.qtbot.waitUntil(lambda: not win.is_exporting(), timeout=timeout)
        self.qtbot.wait(100)

    def export(self, win, path: Path | str | None) -> None:
        self.save_path = None if path is None else str(path)
        self.export_action(win).trigger()
        self.wait_idle(win)

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]

    def of(self, *kinds: str) -> list[dict]:
        return [e for e in self.events if e["kind"] in kinds]

    def notices(self) -> list[str]:
        """Title and text of each message the window showed, other than the warning."""
        return [e["title"] + "\n" + e["text"] for e in self.of("information", "warning", "critical")]

    @staticmethod
    def grid_view(win):
        from PySide6.QtWidgets import QAbstractItemView, QHeaderView

        return next(
            v for v in win.grid_pane.findChildren(QAbstractItemView) if not isinstance(v, QHeaderView)
        )

    def click_object(self, win, oid: str) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        view = self.grid_view(win)
        model = view.model()
        for row in range(model.rowCount()):
            for col in range(model.columnCount()):
                if model.index(row, col).data() == oid:
                    rect = view.visualRect(model.index(row, col))
                    QTest.mouseClick(
                        view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center()
                    )
                    return
        raise AssertionError(f"{oid} isn't a row of the grid")

    @staticmethod
    def inspector_texts(win) -> list[str]:
        from PySide6.QtWidgets import QLabel

        return [w.text() for w in win.inspector.findChildren(QLabel) if w.isVisible()]


@pytest.fixture
def gui(qtbot, monkeypatch):
    monkeypatch.delenv("MELLON_DRAWIO", raising=False)
    return Gui(qtbot, monkeypatch)


def hide_drawio(monkeypatch, tmp_path: Path) -> dict[str, str]:
    """Make draw.io desktop impossible to find: no MELLON_DRAWIO, empty install
    folders and PATH. Returns the same environment for a server process."""
    empty = tmp_path / "no-programs"
    empty.mkdir(exist_ok=True)
    env = {
        "ProgramFiles": str(empty),
        "ProgramW6432": str(empty),
        "ProgramFiles(x86)": str(empty),
        "LOCALAPPDATA": str(empty),
        "PATH": str(empty),
    }
    monkeypatch.delenv("MELLON_DRAWIO", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert find_drawio() is None, "precondition: draw.io desktop must not be found"
    return env


def pdf_standin(tmp_path: Path) -> Path:
    return make_program(tmp_path / "standin-bin", "tectonic", STANDIN_PDF_PROGRAM)


# Stand-in figure programs, called as `-x -f pdf --crop -o <out> <source>`.
FIGURES_WRITE_NOTHING = "import sys\nsys.exit(0)\n"


FIGURES_WRITE_EMPTY_FILES = r'''
import sys
from pathlib import Path
args = sys.argv[1:]
out = Path(args[args.index("-o") + 1])
src = Path(args[-1])
out.mkdir(parents=True, exist_ok=True)
for f in (sorted(src.iterdir()) if src.is_dir() else [src]):
    (out / (f.stem + ".pdf")).write_bytes(b"")
'''


FIGURES_FAIL = "import sys\nsys.stderr.write('stand-in figure program failed\\n')\nsys.exit(1)\n"


def small_repo(root: Path, doc_id: str = "DOC") -> Path:
    write_doc(
        root,
        doc_id,
        [heading(f"{doc_id}-0", "Opening Section", obj(f"{doc_id}-1", "First requirement text."))],
    )
    git_commit_all(root, "doc")
    return root


# ==========================================================================
# What is exported
# ==========================================================================


def _uncommitted_repo(root: Path) -> Path:
    write_doc(root, "DOC", [obj("DOC-1", "Committed wording alpha.")])
    git_commit_all(root, "doc")
    write_object(root / "DOC", obj("DOC-1", "Uncommitted wording beta."))
    return root


# ==========================================================================
# Export warning and link states
# ==========================================================================


def _linked_repo(root: Path, *, broken: bool) -> Path:
    """DOC-1 and DOC-2 link to PAR-1 and PAR-2, acknowledged (OK). With
    `broken`, PAR-2 is then deleted and the deletion committed."""
    write_doc(root, "PAR", [obj("PAR-1", "Parent one."), obj("PAR-2", "Parent two.")])
    ack = short(git_commit_all(root, "parent"))
    write_doc(
        root,
        "DOC",
        [obj("DOC-1", "One.", links=[("PAR", "PAR-1", ack)]), obj("DOC-2", "Two.", links=[("PAR", "PAR-2", ack)])],
        parents=("PAR",),
    )
    git_commit_all(root, "doc")
    if broken:
        write_doc(root, "PAR", [obj("PAR-1", "Parent one.")])
        (root / "PAR" / "objects" / "PAR-2.xml").unlink()
        git_commit_all(root, "delete PAR-2")
    return root


# ==========================================================================
# Figures not drawn
# ==========================================================================


def _figure_repo(root: Path) -> Path:
    write_doc(root, "DOC", [obj("DOC-1", "Has a figure.", figure=plain_svg()), obj("DOC-2", "No figure.")])
    git_commit_all(root, "doc")
    return root


# ==========================================================================
# Failures
# ==========================================================================

OS_REASON = r"denied|directory|access|permission|errno|winerror|exist|taken-by-folder"


@pytest.fixture(scope="module")
def issue_repo(tmp_path_factory):
    """DOC tagged issue 4.2.9, then 4.2.10 on a later commit; 9.9.9 tags a
    commit on another branch, which the latest commit doesn't reach."""
    root = tmp_path_factory.mktemp("issues") / "repo"
    write_doc(root, "DOC", [obj("DOC-1", "Issued text.")])
    first = git_commit_all(root, "first")
    tag_issue(root, "DOC", "4.2.9", first)
    write_doc(root, "DOC", [obj("DOC-1", "Issued text, revised.")])
    second = git_commit_all(root, "second")
    tag_issue(root, "DOC", "4.2.10", second)
    repo = pygit2.Repository(str(root))
    side_tree = repo.get(second).tree
    side = repo.create_commit(None, SIG, SIG, "side branch", side_tree.id, [second])
    repo.branches.local.create("side", repo.get(side))
    tag_issue(root, "DOC", "9.9.9", side)
    return root


def _body(pdf: Pdf) -> str:
    """Squashed text of the pages after the contents page."""
    return squash("\n".join(pdf.texts[2:]))


# ==========================================================================
# Objects and attributes
# ==========================================================================

ALL_ATTRS = {
    "status": "StatDraft",
    "priority": "PrioHigh",
    "verification-method": "VmTest",
    "rationale": "RatUnique",
    "req-type": "RtFunctional",
}


def _attrs_repo(root: Path) -> Path:
    write_doc(
        root,
        "DOC",
        [
            obj("DOC-1", "First object words.", attrs=ALL_ATTRS),
            obj("DOC-2", "Second object words.", attrs={"status": "StatApproved"}),
        ],
    )
    git_commit_all(root, "doc")
    return root


def _object_text(pdf: Pdf, oid: str, next_oid: str | None) -> str:
    """Squashed body text from object `oid`'s ID to the next object's ID."""
    body = _body(pdf)
    start = id_positions(body, oid)
    assert start, f"{oid} isn't shown"
    end = id_positions(body, next_oid) if next_oid else []
    return body[start[0] : end[0] if end else len(body)]


def _check_default_attributes(pdf: Pdf) -> None:
    part = _object_text(pdf, "DOC-1", "DOC-2")
    desc = part.find("Firstobjectwords.")
    assert desc >= 0
    after = part[desc:]
    positions = [after.find(v) for v in ("Requirement", "StatDraft", "PrioHigh", "VmTest")]
    assert all(p >= 0 for p in positions), f"a default attribute is missing: {after!r}"
    assert positions == sorted(positions), "the attributes aren't in the order type, status, priority, verification-method"
    for name, value in (("rationale", "RatUnique"), ("req-type", "RtFunctional")):
        assert value not in part, f"{name} is shown"
        assert name not in part.lower(), f"{name} is shown"


def _broken_parent_repo(root: Path) -> Path:
    write_doc(root, "PAR", [obj("PAR-1", "Parent one.")])
    ack = short(git_commit_all(root, "parent"))
    write_doc(root, "DOC", [obj("DOC-1", "Child one.", links=[("PAR", "PAR-1", ack)])], parents=("PAR",))
    git_commit_all(root, "doc")
    (root / "PAR" / "PAR.xml").write_text('<?xml version="1.0"?>\n<document id="PAR">\n  <outline>\n', encoding="utf-8")
    return root


DIDNT_LOAD = r"(not|n[’']t|fail\w*|unable)\W+(\w+\W+){0,3}load|load\w*\W+(\w+\W+){0,3}(fail\w*|error)"


def _check_names_unloaded_parent(pdf: Pdf) -> None:
    text = strip_page_numbers("\n".join(pdf.texts[2:]))
    start = [m.start() for m in re.finditer(r"Traceability", text)]
    assert start, "no Traceability section"
    section = text[start[0] :]
    lines = [l for l in section.splitlines() if re.search(r"\bPAR\b(?!-)", l)]
    assert lines, f"the section doesn't name PAR: {section!r}"
    assert any(re.search(DIDNT_LOAD, l, re.I) for l in lines), f"no line says PAR didn't load: {lines}"


# ==========================================================================
# General
# ==========================================================================


def _git_state(root: Path, skip: set[str]) -> dict[str, str]:
    """A hash of each working-copy file (other than `skip`), of the git index,
    HEAD, packed-refs and each loose reference, and each reference's target."""
    state = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in skip:
            continue
        if rel.startswith(".git/"):
            inner = rel[len(".git/") :]
            if not (inner in ("index", "HEAD", "packed-refs", "config") or inner.startswith("refs/")):
                continue
        state[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    repo = pygit2.Repository(str(root))
    for name in repo.references:
        ref = repo.references[name]
        state[f"ref:{name}"] = str(ref.target)
    return state


def _status(root: Path) -> dict[str, int]:
    return dict(pygit2.Repository(str(root)).status())


def _clean_repo_for_177(root: Path) -> Path:
    write_doc(root, "PAR", [obj("PAR-1", "Parent one.")])
    ack = short(git_commit_all(root, "parent"))
    write_doc(
        root,
        "DOC",
        [heading("DOC-0", "Opening Section", obj("DOC-1", "Child one.", links=[("PAR", "PAR-1", ack)]))],
        parents=("PAR",),
    )
    git_commit_all(root, "doc")
    tag_issue(root, "DOC", "1", pygit2.Repository(str(root)).head.target)
    assert _status(root) == {}, "precondition: a clean repository"
    return root

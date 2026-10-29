"""draw.io (diagrams.net) integration for figures.

A figure is ``<doc>/figures/<obj-id>.svg``. When draw.io wrote it, the SVG
carries the editable diagram in its root ``content`` attribute, so the same
file is both the picture Mellon shows and the source draw.io desktop opens and
saves back in place.

Standard library + lxml only: the frozen ``mellon-mcp.exe`` ships without
PySide6, and PDF export (``mellon.export``) runs there too.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote
from xml.sax.saxutils import quoteattr

from lxml import etree

DOWNLOAD_URL = "https://www.drawio.com/"


class DrawioError(RuntimeError):
    """Raised when draw.io can't be found or run."""


# An editable draw.io SVG with an empty one-page diagram in it. draw.io opens
# it as a blank diagram and saves the drawing back into the same file.
BLANK_DRAWIO_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" '
    b'xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" '
    b'width="1px" height="1px" viewBox="-0.5 -0.5 1 1" '
    b'content="&lt;mxfile&gt;&lt;diagram id=&quot;page-1&quot; '
    b'name=&quot;Page-1&quot;&gt;&lt;mxGraphModel&gt;&lt;root&gt;'
    b'&lt;mxCell id=&quot;0&quot;/&gt;'
    b'&lt;mxCell id=&quot;1&quot; parent=&quot;0&quot;/&gt;'
    b'&lt;/root&gt;&lt;/mxGraphModel&gt;&lt;/diagram&gt;&lt;/mxfile&gt;">'
    b"<defs/><g/></svg>\n"
)


def _parser() -> etree.XMLParser:
    # draw.io files start with a DOCTYPE pointing at the SVG 1.1 DTD; never
    # fetch it or expand entities from it.
    return etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=True
    )


def _parse(data: bytes) -> etree._Element | None:
    try:
        return etree.fromstring(data, _parser())
    except etree.XMLSyntaxError:
        return None


def _decode_content(content: str) -> str:
    """Diagram XML from an SVG ``content`` attribute.

    Mirrors draw.io's own reader: the attribute is plain XML, base64, or
    URL-encoded (entities are already decoded by the XML parser).
    """
    if content[:1] not in ("<", "%"):
        try:
            content = base64.b64decode(content, validate=False).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return ""
    if content[:1] == "%":
        content = unquote(content)
    return content


def is_drawio_svg(data: bytes) -> bool:
    """True when ``data`` is an SVG with an editable draw.io diagram in it."""
    root = _parse(data)
    if root is None or etree.QName(root).localname != "svg":
        return False
    xml = _decode_content(root.get("content") or "")
    return "<mxfile" in xml or "<mxGraphModel" in xml


def is_drawio_file(path: Path) -> bool:
    """``is_drawio_svg`` for a file; False when it can't be read."""
    try:
        return is_drawio_svg(Path(path).read_bytes())
    except OSError:
        return False


# --- rendering draw.io SVGs with QtSvg ---------------------------------------

_SVG_NS = "http://www.w3.org/2000/svg"
_XHTML_NS = "http://www.w3.org/1999/xhtml"
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_WARNING_HREF = "svg-export-text-problems"
_CSS_FUNCS = ("light-dark(", "var(")
_BLOCK_TAGS = frozenset({"div", "p", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"})
# Room for a label draw.io doesn't wrap (its box is 1px wide).
_NOWRAP_WIDTH = 10000.0
# Average glyph advance as a fraction of the font size, for guessing how many
# lines Qt will wrap a label into (only used to centre it vertically).
_GLYPH_EM = 0.55


def _css_resolve(value: str) -> str:
    """Replace CSS ``light-dark(A, B)`` with ``A`` and ``var(--x, B)`` with ``B``.

    draw.io writes both for its adaptive dark-mode colours; QtSvg understands
    neither and paints such fills black. Arguments may nest (``rgb(...)``,
    ``var(...)``), so this scans parentheses rather than using a regex.
    """
    out: list[str] = []
    i = 0
    while True:
        hits = [(value.find(f, i), f) for f in _CSS_FUNCS]
        hits = [(pos, f) for pos, f in hits if pos >= 0]
        if not hits:
            out.append(value[i:])
            return "".join(out)
        start, func = min(hits)
        out.append(value[i:start])
        j = start + len(func)
        depth, arg_start, args = 1, j, []
        while j < len(value):
            ch = value[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            elif ch == "," and depth == 1:
                args.append(value[arg_start:j])
                arg_start = j + 1
            j += 1
        args.append(value[arg_start:j])
        if func == "light-dark(":
            chosen = args[0]
        else:  # var(--name, fallback): the custom property is never defined
            chosen = ",".join(args[1:])
        out.append(_css_resolve(chosen.strip()))
        i = j + 1


def _css_decls(style: str | None) -> dict[str, str]:
    decls: dict[str, str] = {}
    for part in (style or "").split(";"):
        name, sep, val = part.partition(":")
        if sep:
            decls[name.strip().lower()] = val.strip()
    return decls


def _px(value: str | None, default: float = 0.0) -> float:
    try:
        return float((value or "").strip().removesuffix("px"))
    except ValueError:
        return default


def _label_lines(el: etree._Element) -> list[str]:
    """Text lines of an HTML label: ``<br>`` and block elements break lines."""
    lines: list[str] = [""]

    def brk() -> None:
        if lines[-1].strip():
            lines.append("")

    def walk(node: etree._Element) -> None:
        tag = etree.QName(node).localname if isinstance(node.tag, str) else ""
        if tag == "br":
            lines.append("")
        else:
            if tag in _BLOCK_TAGS:
                brk()
            if node.text:
                lines[-1] += node.text
            for child in node:
                walk(child)
            if tag in _BLOCK_TAGS:
                brk()
        if node.tail:
            lines[-1] += node.tail

    if el.text:
        lines[-1] += el.text
    for child in el:
        walk(child)
    cleaned = [" ".join(line.replace("\xa0", " ").split()) for line in lines]
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return cleaned


def _label_text_area(fo: etree._Element, fallback: etree._Element) -> etree._Element | None:
    """An SVG Tiny ``<textArea>`` standing in for a draw.io HTML label.

    QtSvg wraps a textArea with real font metrics, so multi-line and wrapped
    labels stay readable — unlike draw.io's own fallback, one truncated line.
    Geometry comes from draw.io's flexbox wrapper divs: the outer one holds
    the box (``margin-left``/``width``) and anchor line (``padding-top``),
    the inner one the font.
    """
    divs = [d for d in fo.iter(f"{{{_XHTML_NS}}}div")]
    if len(divs) < 2:
        return None
    outer = _css_decls(divs[0].get("style"))
    inner_div = divs[-1]
    inner = _css_decls(inner_div.get("style"))
    align = _css_decls(divs[1].get("style")).get("text-align", "center")
    lines = _label_lines(inner_div)
    if not lines:
        return None

    font_size = _px(inner.get("font-size"), 12.0) or 12.0
    line_h = font_size * (_px(inner.get("line-height"), 1.2) or 1.2)
    left = _px(outer.get("margin-left"))
    width = _px(outer.get("width"), 1.0)
    justify = outer.get("justify-content", "")
    vertical = outer.get("align-items", "")
    wraps = width > 1 and inner.get("white-space", "normal") != "nowrap"

    if wraps:
        anchor = {"left": "start", "right": "end"}.get(align, "middle")
        x = left
        wrapped = sum(
            max(1, -(-int(len(line) * font_size * _GLYPH_EM) // int(width)))
            for line in lines
        )
    else:
        width = _NOWRAP_WIDTH
        if "flex-start" in justify:
            anchor, x = "start", left
        elif "flex-end" in justify:
            anchor, x = "end", left - width
        else:
            anchor, x = "middle", left - width / 2
        wrapped = len(lines)

    top = _px(outer.get("padding-top"))
    block_h = wrapped * line_h
    if "flex-start" in vertical:
        y = top
    elif "flex-end" in vertical:
        y = top - block_h
    else:
        y = top - block_h / 2

    family = (inner.get("font-family") or fallback.get("font-family") or "Helvetica")
    family = family.split(",")[0].strip().strip("'\"")
    color = inner.get("color") or fallback.get("fill") or "#000000"

    area = etree.Element(f"{{{_SVG_NS}}}textArea")
    for name, val in (
        ("x", f"{x:g}"),
        ("y", f"{y:g}"),
        ("width", f"{width:g}"),
        ("height", "auto"),
        ("font-family", family),
        ("font-size", f"{font_size:g}"),
        ("fill", _css_resolve(color)),
        ("text-anchor", anchor),
        ("line-increment", f"{line_h:g}"),
    ):
        area.set(name, val)
    for n, line in enumerate(lines):
        if n:
            br = etree.SubElement(area, f"{{{_SVG_NS}}}tbreak")
            br.tail = line
        else:
            area.text = line
    return area


def qt_svg(data: bytes) -> bytes:
    """``data`` rewritten so QtSvg draws a draw.io SVG the way draw.io does.

    QtSvg implements SVG Tiny 1.2, so for draw.io exports it:

    - paints ``light-dark()`` / ``var()`` colours black → resolved to the
      light colour;
    - can't render HTML labels (``<foreignObject>``) → recent draw.io stores
      each label's picture as the fallback, which Qt draws fine; older files
      fall back to one truncated ``<text>`` line, replaced here by a wrapping
      ``<textArea>``;
    - shows draw.io's "Text is not SVG - cannot display" notice → dropped.

    Anything that isn't a parseable SVG comes back unchanged.
    """
    root = _parse(data)
    if root is None or etree.QName(root).localname != "svg":
        return data

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for name, val in el.attrib.items():
            if name != "content" and any(f in val for f in _CSS_FUNCS):
                el.set(name, _css_resolve(val))
        if etree.QName(el).localname == "style" and el.text and any(
            f in el.text for f in _CSS_FUNCS
        ):
            el.text = _css_resolve(el.text)

    for switch in list(root.iter(f"{{{_SVG_NS}}}switch")):
        parent = switch.getparent()
        if parent is None:
            continue
        if any(_WARNING_HREF in (a.get(_XLINK_HREF) or a.get("href") or "")
               for a in switch.iter(f"{{{_SVG_NS}}}a")):
            parent.remove(switch)
            continue
        fo = switch.find(f"{{{_SVG_NS}}}foreignObject")
        fallback = switch.find(f"{{{_SVG_NS}}}text")
        if fo is None or fallback is None:
            continue  # an <image> fallback renders as is
        area = _label_text_area(fo, fallback)
        if area is not None:
            area.tail = switch.tail
            parent.replace(switch, area)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def find_drawio() -> str | None:
    """Locate the draw.io desktop executable.

    Search order:
        1. ``MELLON_DRAWIO`` — explicit path, returned as-is (a bad path fails
           loudly at run time rather than being skipped).
        2. The Windows installer locations: per-machine under
           ``%ProgramFiles%``, per-user under ``%LOCALAPPDATA%\\Programs``.
        3. ``PATH`` (``drawio`` from the Linux packages, or ``draw.io``).
    """
    override = os.environ.get("MELLON_DRAWIO")
    if override:
        return override
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            for sub in (("draw.io",), ("Programs", "draw.io")):
                cand = Path(base, *sub, "draw.io.exe")
                if cand.is_file():
                    return str(cand)
    for name in ("drawio", "draw.io"):
        on_path = shutil.which(name)
        if on_path is not None:
            return on_path
    return None


def open_in_drawio(exe: str, path: Path) -> None:
    """Open ``path`` in draw.io desktop and return straight away.

    draw.io saves back into the same file; Mellon notices the change when its
    window is next activated. Raises ``DrawioError`` if draw.io won't start.
    """
    if sys.platform == "win32":
        kwargs: dict = {
            "creationflags": subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
    else:
        kwargs = {"start_new_session": True}
    try:
        subprocess.Popen(
            [exe, str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **kwargs,
        )
    except OSError as e:
        raise DrawioError(f"could not start draw.io at {exe}: {e}") from e


# --- PDF export ----------------------------------------------------------------

# Wall clock limit for one batch export: Electron start-up plus every figure.
_EXPORT_TIMEOUT_S = 300

# CSS absolute units → px, for sizing a plain SVG.
_UNIT_PX = {"": 1.0, "px": 1.0, "pt": 4 / 3, "pc": 16.0, "mm": 96 / 25.4,
            "cm": 96 / 2.54, "in": 96.0}
_DEFAULT_SIZE = (400.0, 300.0)


def _length_px(value: str | None) -> float | None:
    m = re.fullmatch(r"\s*([0-9.]+)\s*([a-z]*)\s*", value or "")
    if m is None or m.group(2) not in _UNIT_PX:
        return None
    try:
        px = float(m.group(1)) * _UNIT_PX[m.group(2)]
    except ValueError:
        return None
    return px if px > 0 else None


def _svg_size(root: etree._Element) -> tuple[float, float]:
    """Intrinsic size of a plain SVG in px: width/height, else the viewBox."""
    w, h = _length_px(root.get("width")), _length_px(root.get("height"))
    box = (root.get("viewBox") or "").replace(",", " ").split()
    if len(box) == 4:
        try:
            bw, bh = float(box[2]), float(box[3])
        except ValueError:
            bw = bh = 0.0
        if bw > 0 and bh > 0:
            if w is None and h is None:
                return bw, bh
            if w is None:
                return h * bw / bh, h
            if h is None:
                return w, w * bh / bw
    if w is not None and h is not None:
        return w, h
    return _DEFAULT_SIZE


def _wrap_plain_svg(data: bytes) -> str | None:
    """A one-cell draw.io diagram showing a plain SVG as a picture.

    draw.io's CLI only exports files with a diagram in them; wrapping lets a
    figure drawn in any tool render through the same path (still vector).
    """
    root = _parse(data)
    if root is None or etree.QName(root).localname != "svg":
        return None
    w, h = _svg_size(root)
    # draw.io's own convention: no ";base64" (";" separates style entries).
    style = (
        "shape=image;noLabel=1;imageAspect=0;aspect=fixed;"
        f"image=data:image/svg+xml,{base64.b64encode(data).decode('ascii')};"
    )
    return (
        '<mxfile><diagram id="figure" name="Page-1"><mxGraphModel><root>'
        '<mxCell id="0"/><mxCell id="1" parent="0"/>'
        f'<mxCell id="2" value="" style={quoteattr(style)} vertex="1" parent="1">'
        f'<mxGeometry width="{w:g}" height="{h:g}" as="geometry"/></mxCell>'
        "</root></mxGraphModel></diagram></mxfile>"
    )


def _run_drawio(exe: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        return subprocess.run(
            [exe, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_EXPORT_TIMEOUT_S,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as e:
        raise DrawioError(
            f"draw.io did not finish exporting within {_EXPORT_TIMEOUT_S} s"
        ) from e
    except OSError as e:
        raise DrawioError(f"could not run draw.io at {exe}: {e}") from e


def export_pdfs(exe: str, svgs: Sequence[Path], out_dir: Path) -> dict[int, Path]:
    """Render figures to cropped vector PDFs in one draw.io run.

    ``svgs[n]`` becomes ``out_dir/fig-<n>.pdf``; the result maps ``n`` to
    the PDF for each figure that rendered. One that didn't (an empty
    diagram, an unreadable file) is simply absent — draw.io carries on past
    it — so callers fall back per figure. A plain SVG is wrapped as a picture
    first (``_wrap_plain_svg``). Multi-page diagrams export their first page.

    Raises ``DrawioError`` only when draw.io can't be run at all.
    """
    if not svgs:
        return {}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mellon-drawio-") as tmp:
        # A folder input exports every file in it with one Electron start-up;
        # neutral names keep object ids out of the command line.
        src = Path(tmp) / "src"
        src.mkdir()
        for n, svg in enumerate(svgs):
            try:
                data = Path(svg).read_bytes()
            except OSError:
                continue
            if is_drawio_svg(data):
                (src / f"fig-{n}.svg").write_bytes(data)
            elif (wrapped := _wrap_plain_svg(data)) is not None:
                (src / f"fig-{n}.drawio").write_text(wrapped, encoding="utf-8")
        if not any(src.iterdir()):
            return {}
        # The exit status is 1 if any one file failed; check outputs instead.
        _run_drawio(exe, ["-x", "-f", "pdf", "--crop", "-o", str(out_dir), str(src)])
    return {
        n: pdf
        for n in range(len(svgs))
        if (pdf := out_dir / f"fig-{n}.pdf").is_file()
    }

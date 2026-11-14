"""draw.io integration: lookup, diagram detection, blank template, launching."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
from lxml import etree

from mellon import drawio


_DIAGRAM = (
    '<mxfile host="Electron"><diagram id="p1" name="Page-1"><mxGraphModel>'
    '<root><mxCell id="0"/><mxCell id="1" parent="0"/></root>'
    "</mxGraphModel></diagram></mxfile>"
)


def _svg_with_content(content: str) -> bytes:
    from xml.sax.saxutils import quoteattr

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="40px" height="20px" '
        f"content={quoteattr(content)}><rect width='40' height='20'/></svg>"
    ).encode("utf-8")


# --- is_drawio_svg -----------------------------------------------------------


def test_is_drawio_svg_plain_content() -> None:
    assert drawio.is_drawio_svg(_svg_with_content(_DIAGRAM))


def test_is_drawio_svg_base64_content() -> None:
    b64 = base64.b64encode(_DIAGRAM.encode()).decode()
    assert drawio.is_drawio_svg(_svg_with_content(b64))


def test_is_drawio_svg_url_encoded_content() -> None:
    assert drawio.is_drawio_svg(_svg_with_content(quote(_DIAGRAM)))


def test_is_drawio_svg_bare_graph_model() -> None:
    assert drawio.is_drawio_svg(
        _svg_with_content("<mxGraphModel><root/></mxGraphModel>")
    )


@pytest.mark.parametrize(
    "data",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>',
        _svg_with_content("not a diagram"),
        b'<html content="&lt;mxfile/&gt;"/>',
        b"not xml at all",
        b"",
    ],
)
def test_is_drawio_svg_rejects(data: bytes) -> None:
    assert not drawio.is_drawio_svg(data)


def test_is_drawio_file_missing_is_false(tmp_path: Path) -> None:
    assert not drawio.is_drawio_file(tmp_path / "nope.svg")


# --- blank template ----------------------------------------------------------


def test_blank_template_is_an_editable_drawio_svg() -> None:
    assert drawio.is_drawio_svg(drawio.BLANK_DRAWIO_SVG)


# --- find_drawio ---------------------------------------------------------------


@pytest.fixture
def no_drawio(monkeypatch, tmp_path):
    """A machine without draw.io: no override, empty install dirs, an empty
    folder as PATH (``tmp_path / "bin"``)."""
    monkeypatch.delenv("MELLON_DRAWIO", raising=False)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    (tmp_path / "bin").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    return tmp_path


def test_find_drawio_env_override_wins(no_drawio, monkeypatch) -> None:
    monkeypatch.setenv("MELLON_DRAWIO", r"D:\tools\draw.io.exe")
    assert drawio.find_drawio() == r"D:\tools\draw.io.exe"


def test_find_drawio_none_when_missing(no_drawio) -> None:
    assert drawio.find_drawio() is None


@pytest.mark.parametrize(
    "parts",
    [("pf", "draw.io"), ("lad", "Programs", "draw.io")],
    ids=["per-machine", "per-user"],
)
def test_find_drawio_windows_install_dirs(no_drawio, monkeypatch, parts) -> None:
    monkeypatch.setattr(drawio.sys, "platform", "win32")
    exe = no_drawio.joinpath(*parts, "draw.io.exe")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert drawio.find_drawio() == str(exe)


def _path_program(folder: Path, name: str) -> Path:
    """A runnable program called ``name`` in ``folder``, as PATH lookup finds it."""
    folder.mkdir(parents=True, exist_ok=True)
    prog = folder / (f"{name}.exe" if sys.platform == "win32" else name)
    prog.write_bytes(b"")
    prog.chmod(0o755)
    return prog


def test_find_drawio_falls_back_to_path(no_drawio, monkeypatch) -> None:
    exe = _path_program(no_drawio / "bin", "drawio")
    assert Path(drawio.find_drawio()) == exe


# MELLON-EDIT-SRS-247: the lookup leaves out the repository and the current
# folder, unless MELLON_DRAWIO names the program.


def test_edit_srs_247_drawio_in_current_folder_is_not_found(no_drawio, monkeypatch) -> None:
    """A draw.io program committed to the repository Mellon was started in
    isn't found (Windows finds a bare name in the current folder first)."""
    repo = no_drawio / "repo"
    _path_program(repo, "drawio")
    _path_program(repo, "draw.io")
    monkeypatch.chdir(repo)
    assert drawio.find_drawio() is None
    assert drawio.find_drawio(repo) is None


def test_edit_srs_247_drawio_inside_repository_is_not_found(no_drawio, monkeypatch) -> None:
    """A PATH entry inside the repository, or a relative one, is left out."""
    repo = no_drawio / "repo"
    _path_program(repo / "node_modules" / ".bin", "drawio")
    _path_program(no_drawio / "start" / "bin", "drawio")
    monkeypatch.chdir(no_drawio / "start")
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(repo / "node_modules" / ".bin"), "bin"])
    )
    assert drawio.find_drawio(repo) is None


@pytest.mark.skipif(sys.platform != "win32", reason="the install folders are Windows places")
def test_edit_srs_247_install_folder_inside_repository_is_left_out(
    no_drawio, monkeypatch
) -> None:
    repo = no_drawio / "repo"
    monkeypatch.setenv("LOCALAPPDATA", str(repo))
    _path_program(repo / "Programs" / "draw.io", "draw.io")
    assert drawio.find_drawio(repo) is None


def test_edit_srs_247_mellon_drawio_in_repository_is_used(no_drawio, monkeypatch) -> None:
    repo = no_drawio / "repo"
    named = _path_program(repo, "drawio")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MELLON_DRAWIO", str(named))
    assert drawio.find_drawio(repo) == str(named)


def test_edit_srs_247_drawio_on_path_outside_repository_is_found(
    no_drawio, monkeypatch
) -> None:
    repo = no_drawio / "repo"
    _path_program(repo, "drawio")
    monkeypatch.chdir(repo)
    outside = _path_program(no_drawio / "bin", "drawio")
    assert Path(drawio.find_drawio(repo)) == outside


# --- open_in_drawio ------------------------------------------------------------


def test_open_in_drawio_launches_detached(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(drawio.subprocess, "Popen", fake_popen)
    fig = tmp_path / "DOC-1.svg"
    drawio.open_in_drawio("draw.io.exe", fig)
    (args, kwargs), = calls
    assert args == ["draw.io.exe", str(fig)]
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_open_in_drawio_wraps_os_error(monkeypatch, tmp_path: Path) -> None:
    def fake_popen(args, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(drawio.subprocess, "Popen", fake_popen)
    with pytest.raises(drawio.DrawioError, match="could not start draw.io"):
        drawio.open_in_drawio("missing.exe", tmp_path / "x.svg")


# --- qt_svg ------------------------------------------------------------------


_SVG = "http://www.w3.org/2000/svg"

_LEGACY_LABEL = (
    '<switch><foreignObject style="overflow: visible; text-align: left;" '
    'pointer-events="none" width="100%" height="100%" '
    'requiredFeatures="http://www.w3.org/TR/SVG11/feature#Extensibility">'
    '<div xmlns="http://www.w3.org/1999/xhtml" style="display: flex; '
    "align-items: unsafe center; justify-content: unsafe center; width: 158px; "
    'height: 1px; padding-top: 40px; margin-left: 1px;">'
    '<div style="box-sizing: border-box; font-size: 0; text-align: center;">'
    '<div style="display: inline-block; font-size: 12px; font-family: Helvetica; '
    "color: light-dark(#000000, #ffffff); line-height: 1.2; white-space: normal;\">"
    "Battery <b>Management</b> System<br/>second line</div></div></div>"
    '</foreignObject><text x="80" y="44" fill="rgb(0, 0, 0)" '
    'font-family="Helvetica" font-size="12px" text-anchor="middle">'
    "Battery Manage...</text></switch>"
)

_NOWRAP_LABEL = (
    '<switch><foreignObject requiredFeatures="http://www.w3.org/TR/SVG11/feature#Extensibility">'
    '<div xmlns="http://www.w3.org/1999/xhtml" style="display: flex; '
    "align-items: unsafe center; justify-content: unsafe center; width: 1px; "
    'height: 1px; padding-top: 40px; margin-left: 220px;">'
    '<div style="text-align: center;"><div style="font-size: 11px; '
    'white-space: nowrap;">CAN bus</div></div></div></foreignObject>'
    '<text x="220" y="43">CAN bus</text></switch>'
)

_IMAGE_LABEL = (
    '<switch><foreignObject requiredFeatures="http://www.w3.org/TR/SVG11/feature#Extensibility">'
    '<div xmlns="http://www.w3.org/1999/xhtml"><div><div>VCU</div></div></div>'
    '</foreignObject><image x="281" y="33" width="118" height="17" '
    'xlink:href="data:image/png;base64,iVBORw0KGgo="/></switch>'
)

_WARNING = (
    '<switch><g requiredFeatures="http://www.w3.org/TR/SVG11/feature#Extensibility"/>'
    '<a transform="translate(0,-5)" '
    'xlink:href="https://www.drawio.com/doc/faq/svg-export-text-problems" target="_blank">'
    '<text text-anchor="middle" font-size="10px" x="50%" y="100%">'
    "Text is not SVG - cannot display</text></a></switch>"
)


def _drawio_svg(body: str, content: str = _DIAGRAM) -> bytes:
    from xml.sax.saxutils import quoteattr

    return (
        f'<svg xmlns="{_SVG}" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="402px" height="182px" content={quoteattr(content)}>'
        f"<g>{body}</g></svg>"
    ).encode("utf-8")


def _qt_root(body: str, content: str = _DIAGRAM) -> etree._Element:
    return etree.fromstring(drawio.qt_svg(_drawio_svg(body, content)))


@pytest.mark.parametrize(
    "value, expected",
    [
        ("light-dark(#ffffff, #121212)", "#ffffff"),
        ("light-dark(rgb(218, 232, 252), rgb(29, 41, 59))", "rgb(218, 232, 252)"),
        ("light-dark(#ffffff, var(--ge-dark-color, #121212))", "#ffffff"),
        ("var(--ge-adaptive-bg, #ffffff)", "#ffffff"),
        ("var(--x, light-dark(#aaa, #bbb))", "#aaa"),
        (
            "fill: light-dark(#fff, #000); stroke: var(--s, rgb(1, 2, 3));",
            "fill: #fff; stroke: rgb(1, 2, 3);",
        ),
        ("#123456", "#123456"),
    ],
)
def test_css_resolve(value: str, expected: str) -> None:
    assert drawio._css_resolve(value) == expected


def test_qt_svg_resolves_adaptive_colours_but_not_content() -> None:
    content = "<mxfile><diagram>fillColor=light-dark(#fff,#000)</diagram></mxfile>"
    root = _qt_root(
        '<rect fill="#dae8fc" style="fill: light-dark(rgb(218, 232, 252), '
        'rgb(29, 41, 59)); stroke: var(--ge-adaptive-bg, #ffffff);"/>'
        "<style>@supports (color: light-dark(#000, #fff)) { #x { --c: light-dark(#fff, #000); } }</style>",
        content,
    )
    rect = root.find(f".//{{{_SVG}}}rect")
    assert rect.get("style") == "fill: rgb(218, 232, 252); stroke: #ffffff;"
    assert "light-dark" not in root.find(f".//{{{_SVG}}}style").text
    assert root.get("content") == content


def test_qt_svg_drops_text_is_not_svg_notice() -> None:
    root = _qt_root(_WARNING)
    assert root.find(f".//{{{_SVG}}}switch") is None
    assert b"cannot display" not in etree.tostring(root)


def test_qt_svg_keeps_picture_fallbacks() -> None:
    root = _qt_root(_IMAGE_LABEL)
    switch = root.find(f".//{{{_SVG}}}switch")
    assert switch is not None and switch.find(f"{{{_SVG}}}image") is not None


def test_qt_svg_turns_text_fallback_into_wrapping_text_area() -> None:
    root = _qt_root(_LEGACY_LABEL)
    assert root.find(f".//{{{_SVG}}}switch") is None
    area = root.find(f".//{{{_SVG}}}textArea")
    assert area is not None
    # Every line of the HTML label, not the truncated fallback.
    assert area.text == "Battery Management System"
    (brk,) = area.findall(f"{{{_SVG}}}tbreak")
    assert brk.tail == "second line"
    # Box from the flexbox wrapper: x=margin-left, width=width, centred.
    assert (area.get("x"), area.get("width")) == ("1", "158")
    assert area.get("text-anchor") == "middle"
    assert area.get("font-size") == "12"
    assert area.get("font-family") == "Helvetica"
    assert area.get("fill") == "#000000"
    # Centred around padding-top: the block starts above y=40.
    assert float(area.get("y")) < 40


def test_qt_svg_nowrap_label_centres_on_its_anchor() -> None:
    area = _qt_root(_NOWRAP_LABEL).find(f".//{{{_SVG}}}textArea")
    assert area.text == "CAN bus"
    width = float(area.get("width"))
    assert float(area.get("x")) + width / 2 == 220
    assert area.get("text-anchor") == "middle"


@pytest.mark.parametrize("data", [b"not xml", b"<html/>", b""])
def test_qt_svg_leaves_non_svg_alone(data: bytes) -> None:
    assert drawio.qt_svg(data) == data


# --- export_pdfs ---------------------------------------------------------------


class _Run:
    """Stands in for subprocess.run: records the call, writes chosen PDFs."""

    def __init__(self, render: set[str]) -> None:
        self.render = render
        self.calls: list[list[str]] = []
        self.inputs: dict[str, bytes] = {}

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        out_dir, src = Path(cmd[cmd.index("-o") + 1]), Path(cmd[-1])
        for f in sorted(src.iterdir()):
            self.inputs[f.name] = f.read_bytes()
            if f.stem in self.render:
                (out_dir / f"{f.stem}.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
        return subprocess.CompletedProcess(cmd, 1, "", "")


def test_export_pdfs_one_run_and_per_figure_results(monkeypatch, tmp_path: Path) -> None:
    good = tmp_path / "a.svg"
    good.write_bytes(_svg_with_content(_DIAGRAM))
    plain = tmp_path / "b.svg"
    plain.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"/>')
    fake = _Run(render={"fig-0", "fig-1"})
    monkeypatch.setattr(drawio.subprocess, "run", fake)

    out = drawio.export_pdfs("draw.io.exe", [good, plain, tmp_path / "gone.svg"], tmp_path / "out")

    (cmd,) = fake.calls
    assert cmd[:6] == ["draw.io.exe", "-x", "-f", "pdf", "--crop", "-o"]
    # The draw.io SVG goes in as is; the plain one wrapped as a picture.
    assert fake.inputs["fig-0.svg"] == good.read_bytes()
    assert b"shape=image" in fake.inputs["fig-1.drawio"]
    assert "fig-2.svg" not in fake.inputs
    # The non-zero exit status doesn't matter; the files that exist do.
    assert sorted(out) == [0, 1]
    assert out[0] == tmp_path / "out" / "fig-0.pdf"


def test_export_pdfs_skips_drawio_when_nothing_to_render(monkeypatch, tmp_path: Path) -> None:
    fake = _Run(render=set())
    monkeypatch.setattr(drawio.subprocess, "run", fake)
    assert drawio.export_pdfs("draw.io.exe", [], tmp_path) == {}
    assert drawio.export_pdfs("draw.io.exe", [tmp_path / "gone.svg"], tmp_path) == {}
    assert fake.calls == []


def test_export_pdfs_timeout_raises(monkeypatch, tmp_path: Path) -> None:
    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(drawio.subprocess, "run", slow)
    svg = tmp_path / "a.svg"
    svg.write_bytes(_svg_with_content(_DIAGRAM))
    with pytest.raises(drawio.DrawioError, match="did not finish"):
        drawio.export_pdfs("draw.io.exe", [svg], tmp_path / "out")


@pytest.mark.parametrize(
    "attrs, size",
    [
        ('width="200" height="100"', (200, 100)),
        ('width="200px" height="100px"', (200, 100)),
        ('width="72pt" height="36pt"', (96, 48)),
        ('viewBox="0 0 300 150"', (300, 150)),
        ('width="600" viewBox="0 0 300 150"', (600, 300)),
        ('width="50%" height="100%"', drawio._DEFAULT_SIZE),
        ("", drawio._DEFAULT_SIZE),
    ],
)
def test_svg_size(attrs: str, size) -> None:
    root = etree.fromstring(f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}/>')
    assert drawio._svg_size(root) == pytest.approx(size)


def test_wrap_plain_svg_sizes_the_picture() -> None:
    xml = drawio._wrap_plain_svg(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"/>'
    )
    geo = etree.fromstring(xml).find(".//mxGeometry")
    assert (geo.get("width"), geo.get("height")) == ("200", "100")
    assert drawio._wrap_plain_svg(b"<html/>") is None


# MELLON-EDIT-SRS blind tests (176 and up)

from tests.edit_b_support import _fake_program  # noqa: E402


def _drawio_places(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """Point every place Mellon looks at an empty folder under tmp_path, and
    return where a program would go in each."""
    exe = "draw.io.exe" if sys.platform == "win32" else "drawio"
    pf = tmp_path / "pf"
    la = tmp_path / "la"
    bin_dir = tmp_path / "bin"
    for d in (pf, la, bin_dir):
        d.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("MELLON_DRAWIO", raising=False)
    monkeypatch.setenv("ProgramFiles", str(pf))
    monkeypatch.setenv("ProgramW6432", str(pf))
    monkeypatch.setenv("ProgramFiles(x86)", str(pf))
    monkeypatch.setenv("LOCALAPPDATA", str(la))
    monkeypatch.setenv("PATH", str(bin_dir))
    path_exe = "drawio.exe" if sys.platform == "win32" else "drawio"
    return {
        "env": tmp_path / "custom" / exe,
        "programfiles": pf / "draw.io" / exe,
        "localappdata": la / "Programs" / "draw.io" / exe,
        "path": bin_dir / path_exe,
    }


def _same(found, expected: Path) -> bool:
    return found is not None and Path(found).resolve() == expected.resolve()


def test_edit_srs_246_mellon_drawio_wins_over_programfiles(tmp_path, monkeypatch):
    """MELLON-EDIT-SRS-246 (a): MELLON_DRAWIO names a program and
    %ProgramFiles%\\draw.io holds one: the MELLON_DRAWIO program is found."""
    from mellon.drawio import find_drawio

    places = _drawio_places(tmp_path, monkeypatch)
    _fake_program(places["env"])
    _fake_program(places["programfiles"])
    _fake_program(places["path"])
    monkeypatch.setenv("MELLON_DRAWIO", str(places["env"]))
    found = find_drawio()
    assert _same(found, places["env"]), found


@pytest.mark.skipif(sys.platform != "win32",
                    reason="%ProgramFiles% and %LOCALAPPDATA% are Windows places")
def test_edit_srs_246_programfiles_wins_over_localappdata(tmp_path, monkeypatch):
    """MELLON-EDIT-SRS-246 (b): %ProgramFiles%\\draw.io and
    %LOCALAPPDATA%\\Programs\\draw.io both hold one: %ProgramFiles%'s."""
    from mellon.drawio import find_drawio

    places = _drawio_places(tmp_path, monkeypatch)
    _fake_program(places["programfiles"])
    _fake_program(places["localappdata"])
    _fake_program(places["path"])
    found = find_drawio()
    assert _same(found, places["programfiles"]), found


@pytest.mark.skipif(sys.platform != "win32",
                    reason="%LOCALAPPDATA% is a Windows place")
def test_edit_srs_246_localappdata_wins_over_path(tmp_path, monkeypatch):
    """MELLON-EDIT-SRS-246 (c): %LOCALAPPDATA%\\Programs\\draw.io and PATH
    both hold one: %LOCALAPPDATA%'s."""
    from mellon.drawio import find_drawio

    places = _drawio_places(tmp_path, monkeypatch)
    _fake_program(places["localappdata"])
    _fake_program(places["path"])
    found = find_drawio()
    assert _same(found, places["localappdata"]), found


def test_edit_srs_246_path_is_last_place(tmp_path, monkeypatch):
    """MELLON-EDIT-SRS-246: with only PATH holding draw.io, it is found on
    PATH; with no place holding one, nothing is found."""
    from mellon.drawio import find_drawio

    places = _drawio_places(tmp_path, monkeypatch)
    assert find_drawio() is None
    _fake_program(places["path"])
    assert _same(find_drawio(), places["path"])


# MELLON-PDF-SRS blind tests
import re  # noqa: E402
from xml.sax.saxutils import escape as _xml_escape  # noqa: E402

from mellon.drawio import find_drawio, is_drawio_svg  # noqa: E402
from tests.pdf_support import (  # noqa: E402
    PDF_PROGRAM,
    Pdf,
    _body,
    drawing,
    git_commit_all,
    horizontal_rules,
    mcp_export,
    needs_pdf,
    obj,
    plain_svg,
    squash,
    write_doc,
)


DRAWIO = find_drawio()


needs_drawio = pytest.mark.skipif(
    DRAWIO is None, reason="draw.io desktop wasn't found: install it or set MELLON_DRAWIO"
)


def drawio_svg(*pages: tuple[str, str], width: int = 160, height: int = 60) -> bytes:
    """A draw.io figure whose diagram has one figure page per (name, label),
    each page one box of width x height holding the label."""
    diagrams = []
    for i, (name, label) in enumerate(pages):
        diagrams.append(
            f'<diagram id="p{i}" name="{name}"><mxGraphModel><root>'
            f'<mxCell id="0"/><mxCell id="1" parent="0"/>'
            f'<mxCell id="b{i}" value="{label}" style="rounded=0;whiteSpace=wrap;html=1;" '
            f'vertex="1" parent="1"><mxGeometry x="0" y="0" width="{width}" height="{height}" '
            f'as="geometry"/></mxCell></root></mxGraphModel></diagram>'
        )
    mxfile = f'<mxfile host="Electron" pages="{len(pages)}">{"".join(diagrams)}</mxfile>'
    first_label = pages[0][1]
    # Double-quoted, as draw.io writes it: draw.io reads no diagram from a
    # single-quoted content attribute.
    content = _xml_escape(mxfile, {'"': "&quot;"})
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width + 2}px" height="{height + 2}px" '
        f'viewBox="-0.5 -0.5 {width + 2} {height + 2}" content="{content}">'
        f'<g><rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" stroke="#000000"/>'
        f'<text x="10" y="30">{_xml_escape(first_label)}</text></g></svg>'
    ).encode()


# ==========================================================================
# Figures (draw.io desktop and the PDF program)
# ==========================================================================

FIGURE_CAPTIONS = {
    "DOC-3": "Caption of the multipage figure",
    "DOC-1": "Caption of the drawio figure",
    "DOC-5": "Caption of the tall figure",
    "DOC-2": "Caption of the plain figure",
    "DOC-4": "Caption of the wide figure",
}  # in outline order


@pytest.fixture(scope="module")
def figures_export(tmp_path_factory):
    if PDF_PROGRAM is None:
        pytest.skip("the PDF program (tectonic) wasn't found: set MELLON_TECTONIC or put tectonic on PATH")
    if DRAWIO is None:
        pytest.skip("draw.io desktop wasn't found: install it or set MELLON_DRAWIO")
    root = tmp_path_factory.mktemp("figures") / "repo"
    figures = {
        "DOC-3": drawio_svg(("Page-1", "PAGE-ONE"), ("Page-2", "PAGE-TWO")),
        "DOC-1": drawio_svg(("Page-1", "ZORBLAX")),
        "DOC-5": plain_svg(1000, 3000),
        "DOC-2": plain_svg(240, 120),
        "DOC-4": plain_svg(3000, 1000),
    }
    assert is_drawio_svg(figures["DOC-1"]) and is_drawio_svg(figures["DOC-3"])
    assert not is_drawio_svg(figures["DOC-2"])
    write_doc(root, "DOC", [obj(oid, caption, figure=figures[oid]) for oid, caption in FIGURE_CAPTIONS.items()])
    git_commit_all(root, "doc")
    out = root.parent / "figures.pdf"
    res = mcp_export(root, "DOC", out, env={"MELLON_DRAWIO": DRAWIO})
    assert res["ok"], res
    return res, Pdf(out)


@needs_pdf
@needs_drawio
def test_pdf_srs_149_figures_drawn_when_figure_program_found(figures_export):
    """MELLON-PDF-SRS-149: with the figure program found and able to draw them,
    the PDF draws each figure, draw.io and plain SVG alike: none reported as not
    drawn, no placeholder, and the draw.io figure's label is in the PDF."""
    res, pdf = figures_export
    assert not res.get("unrendered_figures"), res
    text = pdf.flat()
    for oid in FIGURE_CAPTIONS:
        assert f"{oid}.svg" not in text, f"a placeholder stands for {oid}'s figure"
    assert "ZORBLAX" in text, "the draw.io figure's label isn't in the PDF"
    drawn = sum(len(drawing(pdf, i)[1]) for i in range(len(pdf.pages)))
    assert drawn == len(FIGURE_CAPTIONS), f"{drawn} figures drawn"


def _captions(pdf: Pdf) -> list[tuple[int, str]]:
    body = _body(pdf)
    out = []
    for oid, caption in FIGURE_CAPTIONS.items():
        m = re.search(r"Figure(\d+):" + re.escape(squash(caption)), body)
        out.append((int(m.group(1)) if m else -1, oid))
    return out


@needs_pdf
@needs_drawio
def test_pdf_srs_150_drawn_figures_numbered_from_1_in_order(figures_export):
    """MELLON-PDF-SRS-150: drawn figures are numbered from 1 in the order they
    appear (outline order, not ID order)."""
    _, pdf = figures_export
    numbers = [n for n, _ in _captions(pdf)]
    assert numbers == list(range(1, len(FIGURE_CAPTIONS) + 1)), list(zip(numbers, FIGURE_CAPTIONS))


@needs_pdf
@needs_drawio
def test_pdf_srs_152_figure_captioned_with_the_description_shown_once(figures_export):
    """MELLON-PDF-SRS-152: on a requirement, the figure is shown in place of the
    description, captioned 'Figure N:' and the description, which isn't shown
    again."""
    _, pdf = figures_export
    body = _body(pdf)
    for oid, caption in FIGURE_CAPTIONS.items():
        assert re.search(r"Figure\d+:" + re.escape(squash(caption)), body), f"{oid}: no 'Figure N:' caption"
        assert body.count(squash(caption)) == 1, f"{oid}: the description is shown more than once"


def _text_area(pdf: Pdf, index: int) -> tuple[float, float, float, float]:
    """(left, bottom, right, top) of page `index`'s text area: across the
    page's long horizontal rules, between the header rule and the footer."""
    rules = horizontal_rules(pdf, index)
    assert rules, "precondition: the page draws rules that show the text width"
    height = float(pdf.pages[index].mediabox.height)
    left, right = min(r[0] for r in rules), max(r[1] for r in rules)
    head = [r[2] for r in rules if r[2] > 0.75 * height]
    top = max(head) if head else height
    footer = [y for _, _, y in pdf.fragments(index) if y < 0.1 * height]
    bottom = max(footer) if footer else 0.0
    return left, bottom, right, top


@needs_pdf
@needs_drawio
@pytest.mark.parametrize("oid, ratio", [("DOC-4", 3.0), ("DOC-5", 1 / 3)], ids=["wide", "tall"])
def test_pdf_srs_154_large_figure_scaled_to_fit_keeping_proportions(figures_export, oid, ratio):
    """MELLON-PDF-SRS-154: a figure wider (3000 x 1000 px) or taller
    (1000 x 3000 px) than the text area is scaled down to fit the text area,
    keeping its proportions."""
    _, pdf = figures_export
    caption = squash(FIGURE_CAPTIONS[oid])
    index = next(i for i in range(2, len(pdf.pages)) if caption in squash(pdf.texts[i]))
    forms = drawing(pdf, index)[1]
    assert forms, f"precondition: {oid}'s figure is drawn on page {index + 1}"
    candidates = [f for f in forms if abs((f["bbox"][2] - f["bbox"][0]) / (f["bbox"][3] - f["bbox"][1]) - ratio) / ratio < 0.02]
    assert candidates, f"no drawn figure with the proportions {ratio:.2f}"
    x0, y0, x1, y1 = candidates[0]["box"]
    left, bottom, right, top = _text_area(pdf, index)
    tol = 1.0
    assert x0 >= left - tol and x1 <= right + tol, f"figure spans x {x0:.1f}..{x1:.1f}, text area {left:.1f}..{right:.1f}"
    assert y0 >= bottom - tol and y1 <= top + tol, f"figure spans y {y0:.1f}..{y1:.1f}, text area {bottom:.1f}..{top:.1f}"
    placed = (x1 - x0) / (y1 - y0)
    assert abs(placed - ratio) / ratio < 0.02, f"placed with proportions {placed:.3f}, figure {ratio:.3f}"


@needs_pdf
@needs_drawio
def test_pdf_srs_156_multipage_drawio_figure_draws_its_first_page(figures_export):
    """MELLON-PDF-SRS-156: of a draw.io figure whose diagram has two figure
    pages, the PDF draws the first: PAGE-ONE is in the PDF, PAGE-TWO isn't."""
    _, pdf = figures_export
    text = pdf.flat()
    assert "PAGE-ONE" in text, "the first figure page isn't drawn"
    assert "PAGE-TWO" not in text, "the second figure page is drawn"


# --- code view findings (CV-STORE-*) ---


@pytest.mark.parametrize("data", [
    pytest.param(_svg_with_content("<diagrams><mxfile/></diagrams>"), id="other-root-holding-mxfile"),
    pytest.param(_svg_with_content("<mxfileX"), id="not-xml"),
    pytest.param(_svg_with_content("text <mxfile/> more"), id="mxfile-inside-text"),
    pytest.param(_svg_with_content(_DIAGRAM).replace(b"http://www.w3.org/2000/svg",
                                                     b"urn:not-svg"), id="svg-in-another-namespace"),
])
def test_store_srs_132_only_an_mxfile_root_in_an_svg_is_a_drawio_figure(data) -> None:
    """CV-STORE-08, MELLON-STORE-SRS-132: a figure is a draw.io figure when the file's
    root svg element has a content attribute whose value is XML with an mxfile root
    element; these aren't."""
    assert not drawio.is_drawio_svg(data)

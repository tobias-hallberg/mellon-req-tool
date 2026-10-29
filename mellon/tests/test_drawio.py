"""draw.io integration: lookup, diagram detection, blank template, launching."""

from __future__ import annotations

import base64
import subprocess
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
    """A machine without draw.io: no override, empty install dirs, empty PATH."""
    monkeypatch.delenv("MELLON_DRAWIO", raising=False)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    monkeypatch.setattr(drawio.shutil, "which", lambda name: None)
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


def test_find_drawio_falls_back_to_path(no_drawio, monkeypatch) -> None:
    monkeypatch.setattr(
        drawio.shutil, "which", lambda name: "/usr/bin/drawio" if name == "drawio" else None
    )
    assert drawio.find_drawio() == "/usr/bin/drawio"


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
                (out_dir / f"{f.stem}.pdf").write_bytes(b"%PDF-1.7")
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

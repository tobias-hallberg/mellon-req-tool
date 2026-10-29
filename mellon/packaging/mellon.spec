# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — run via packaging/build.ps1, not directly.
#
# Two executables (windowed GUI + console MCP server) sharing one onedir
# bundle: dist/Mellon/{mellon.exe, mellon-mcp.exe, _internal/}.
import os

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

# Drop the software-OpenGL fallback and Qt's .qm translations: Mellon uses
# only QtCore/QtGui/QtWidgets/QtSvg (raster painting, no QOpenGLWidget/QtQuick)
# and never installs a QTranslator. Set MELLON_PYI_SLIM=0 to keep them.
SLIM = os.environ.get("MELLON_PYI_SLIM", "1") != "0"

# Optional .ico rendered from the branding SVG by build.ps1.
ICON = os.environ.get("MELLON_ICON") or None

# templates/syrs/*, gui/icons/*.svg, gui/branding/*.svg, py.typed, plus the
# dist-info so importlib.metadata.version("mellon") resolves __version__.
datas = collect_data_files("mellon") + copy_metadata("mellon")
# pygit2._libgit2 is a compiled cffi module; its C-level import of
# _cffi_backend is invisible to PyInstaller's import scanner.
hiddenimports = ["_cffi_backend"]

gui_a = Analysis(
    [os.path.join(SPECPATH, "launch_gui.py")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        "mcp", "pydantic", "pydantic_core", "uvicorn", "starlette", "httpx",
        # PySide6/__init__ imports QtNetwork only behind an ImportError guard;
        # left in, its hook also drags a foreign OpenSSL off PATH that can
        # shadow CPython's libcrypto-3-x64.dll in the shared _internal/.
        "PySide6.QtNetwork",
        "tkinter",
    ],
    noarchive=False,
)
mcp_a = Analysis(
    [os.path.join(SPECPATH, "launch_mcp.py")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["PySide6", "shiboken6", "tkinter"],
    noarchive=False,
)


def _slim(toc):
    if not SLIM:
        return toc
    keep = []
    for entry in toc:
        dest = entry[0].replace("\\", "/").lower()
        if dest.endswith("/opengl32sw.dll"):
            continue
        if dest.startswith("pyside6/translations/"):
            continue
        # TUIO touch plugin is the only thing left linking Qt6Network.dll.
        if dest.startswith("pyside6/plugins/generic/") or dest == "pyside6/qt6network.dll":
            continue
        keep.append(entry)
    return keep


gui_a.binaries = _slim(gui_a.binaries)
gui_a.datas = _slim(gui_a.datas)

gui_pyz = PYZ(gui_a.pure)
mcp_pyz = PYZ(mcp_a.pure)

gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name="mellon",
    console=False,
    icon=ICON,
    upx=False,
)
mcp_exe = EXE(
    mcp_pyz,
    mcp_a.scripts,
    [],
    exclude_binaries=True,
    name="mellon-mcp",
    console=True,
    icon=ICON,
    upx=False,
)

coll = COLLECT(
    gui_exe,
    gui_a.binaries,
    gui_a.datas,
    mcp_exe,
    mcp_a.binaries,
    mcp_a.datas,
    upx=False,
    name="Mellon",
)

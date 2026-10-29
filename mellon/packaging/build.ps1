<#
.SYNOPSIS
    Build Mellon-Setup-<version>.exe: frozen GUI + MCP server + tectonic.

.DESCRIPTION
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1

    Needs uv, git, Inno Setup 6 and internet access (PyPI, the tectonic
    release and its TeX bundle). Everything is created under build\ in the
    package directory; the installer lands in build\Mellon-Setup-<version>.exe.
#>
param(
    [string]$Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
)

$ErrorActionPreference = 'Stop'

$TectonicVersion = '0.17.0'
$TectonicSha256 = 'f61ce51f0b0ade1015b7de7ef368541c5424e9756ecbd0d7af97d6d48030845f'
$TectonicUrl = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40$TectonicVersion/tectonic-$TectonicVersion-x86_64-pc-windows-msvc.zip"
$PyInstallerVersion = '6.22.3'

$Pkg = Split-Path -Parent $PSScriptRoot      # ...\mellon\mellon (pyproject.toml)
$Root = Split-Path -Parent $Pkg              # outer repo (req-test.bundle)
$Build = Join-Path $Pkg 'build'

function Exec([scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "command failed with exit code ${LASTEXITCODE}: $Command" }
}

if (-not (Test-Path $Iscc)) { throw "Inno Setup not found at $Iscc (pass -Iscc <path to ISCC.exe>)" }

# Build with a minimal PATH: PyInstaller hooks copy DLLs they find on PATH, and
# a foreign OpenSSL (e.g. from a password manager) once replaced CPython's.
$uv = (Get-Command uv).Source
$git = (Get-Command git).Source
$env:PATH = @(
    "$env:SystemRoot\system32", $env:SystemRoot, "$env:SystemRoot\System32\Wbem",
    (Split-Path $uv), (Split-Path $git)
) -join ';'

if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
New-Item -ItemType Directory $Build | Out-Null

Push-Location $Pkg
try {
    Write-Host '==> Build environment (locked runtime dependencies + PyInstaller)'
    Exec { uv export --frozen --no-dev --no-emit-project --no-hashes -o "$Build\requirements.txt" }
    Exec { uv venv --python 3.12 "$Build\venv" }
    $py = "$Build\venv\Scripts\python.exe"
    Exec { uv pip install --python $py -r "$Build\requirements.txt" "pyinstaller==$PyInstallerVersion" }
    Exec { uv pip install --python $py --no-deps . }
    $Version = (& $py -c 'import mellon; print(mellon.__version__)').Trim()
    Write-Host "    mellon $Version"

    Write-Host '==> Application icon'
    $env:QT_QPA_PLATFORM = 'offscreen'
    $Ico = "$Build\mellon.ico"
    Exec { & $py -c @"
import sys
from importlib.resources import files
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
app = QGuiApplication([])
svg = files('mellon.gui.branding').joinpath('mellon-app-icon-1024.svg').read_bytes()
img = QImage(256, 256, QImage.Format.Format_ARGB32)
img.fill(Qt.GlobalColor.transparent)
p = QPainter(img)
QSvgRenderer(svg).render(p)
p.end()
sys.exit(0 if img.save(sys.argv[1], 'ICO') else 1)
"@ $Ico }
    Remove-Item Env:\QT_QPA_PLATFORM

    Write-Host '==> PyInstaller'
    $env:PYINSTALLER_CONFIG_DIR = "$Build\pyinstaller-config"
    $env:MELLON_ICON = $Ico
    Exec { & "$Build\venv\Scripts\pyinstaller.exe" "$PSScriptRoot\mellon.spec" --distpath "$Build\dist" --workpath "$Build\work" --noconfirm }
    $App = "$Build\dist\Mellon"
    Exec { & "$App\mellon-mcp.exe" --version }

    Write-Host "==> tectonic $TectonicVersion"
    $Tools = "$App\tools"
    New-Item -ItemType Directory $Tools | Out-Null
    $zip = "$Build\tectonic.zip"
    Exec { curl.exe -fsSL -o $zip $TectonicUrl }
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $TectonicSha256) { throw "tectonic checksum mismatch: $hash" }
    Expand-Archive $zip -DestinationPath $Tools

    Write-Host '==> Pre-warm the TeX cache (first export then works offline)'
    $env:TECTONIC_CACHE_DIR = "$Tools\tectonic-cache"
    $env:MELLON_TECTONIC = "$Tools\tectonic.exe"
    Exec { git clone --quiet "$Root\req-test.bundle" "$Build\req-test" }
    Exec { & $py -c @"
import sys
from pathlib import Path
from mellon import export
from mellon.repo import load_all
root = Path(sys.argv[1])
docs = load_all(root)
for doc_id, d in sorted(docs.items()):
    # With parents, the PDF ends with the Traceability section, whose
    # link table uses fonts the rest of the document doesn't.
    parents = {p: docs[p].document for p in d.document.parents if p in docs}
    blob = export.pdf(d.document, parents=parents)
    print('    exporting', doc_id, len(blob), 'bytes')
# A page break inside a 20 pt heading makes LaTeX's output routine select
# roman at 20 pt (\normalfont runs before \normalsize), loading lmroman17.
# Whether a document hits that depends on its layout, so fetch it here.
export.build_pdf(r'\documentclass{syrs}\begin{document}{\fontsize{20pt}{24pt}\selectfont x}\end{document}')
"@ "$Build\req-test" }
    Remove-Item Env:\TECTONIC_CACHE_DIR, Env:\MELLON_TECTONIC

    Write-Host '==> Licence + docs'
    Copy-Item "$Pkg\LICENSE", "$PSScriptRoot\THIRD-PARTY-NOTICES.txt", "$Pkg\TEAM.md" $App

    Write-Host '==> Installer'
    Exec { & $Iscc /Q "/DAppVersion=$Version" "/DSourceDir=$App" "/O$Build" "$PSScriptRoot\mellon.iss" }
    Write-Host "Done: $Build\Mellon-Setup-$Version.exe"
}
finally {
    Pop-Location
}

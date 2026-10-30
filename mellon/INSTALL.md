# Installing Mellon

| You are… | Go to |
|---|---|
| On Windows and editing requirements | [Install from Setup.exe](#install-from-setupexe-windows-recommended) |
| Working on Mellon itself, or on Linux/macOS | [Developer setup](#developer-setup-uv-any-os) |
| Building the Windows installer | [Building the installer](#building-the-installer) |

Mellon only **commits**. Pulling, pushing and merging happen in your own git
client; the team routine is in [TEAM.md](TEAM.md). Mellon is free software under
GPL-3.0-or-later, © Tobias Hallberg.

---

## Install from Setup.exe (Windows, recommended)

### Before you install

- 64-bit Windows 10 or 11. No admin rights and no Python needed.
- **Git for Windows** (<https://git-scm.com/download/win>) and a git client you
  are comfortable with (GitHub Desktop, TortoiseGit, Fork, or the command line).
- **Your git identity.** Mellon refuses to commit without it:

  ```
  git config --global user.name  "Your Name"
  git config --global user.email "you@example.com"
  ```

- **A clone of your requirements repository** on a local disk (not
  OneDrive or a network drive). It must be a full clone, not a shallow one.
- **draw.io desktop** (<https://www.drawio.com/>), if you draw or export
  figures. Mellon opens figures in it for editing, and PDF export uses it to
  render them. The standard per-machine or per-user install is found
  automatically; anywhere else, set `MELLON_DRAWIO` to the path of
  `draw.io.exe`. Without it, Mellon still works: figures can't be edited from
  Mellon and print as a placeholder line in PDFs.

### Run the installer

1. Download `Mellon-Setup-<version>.exe` from the
   [Releases page](https://github.com/tobias-hallberg/mellon-req-tool/releases),
   or [build it yourself](#building-the-installer), and run it. It installs for
   the current user only: no admin prompt and no folder choice. The install
   folder is always `%LOCALAPPDATA%\Programs\Mellon`, and the MCP configs below
   rely on that.
2. If Windows shows **"Windows protected your PC"** (SmartScreen), choose
   **More info → Run anyway**. The installer and the exes are not code-signed,
   so SmartScreen asks about downloaded copies. Run from a network share that
   isn't in the Local intranet zone, Setup may instead show an "Unknown
   Publisher" security warning; choose **Run**.
3. Accept the licence (GPL-3.0-or-later) and finish. The last page can start
   Mellon. After that, use the **Mellon** entry in the Start menu.

What gets installed in `%LOCALAPPDATA%\Programs\Mellon`:

| Path | Contents |
|---|---|
| `mellon.exe` | The GUI |
| `mellon-mcp.exe` | The MCP server, a console program that your MCP client starts |
| `tools\tectonic.exe`, `tools\tectonic-cache\` | Tectonic 0.17.0 and a pre-filled TeX cache, for PDF export |
| `_internal\` | Python runtime, Qt and the other libraries |
| `LICENSE`, `THIRD-PARTY-NOTICES.txt`, `TEAM.md` | Licence texts and the team routine |

Nothing is added to `PATH`. To check the install from a terminal, run the
command below. It prints `mellon-mcp <version>`.

cmd:

```
"%LOCALAPPDATA%\Programs\Mellon\mellon-mcp.exe" --version
```

PowerShell:

```
& "$env:LOCALAPPDATA\Programs\Mellon\mellon-mcp.exe" --version
```

### Upgrade or uninstall

- **Upgrade:** first close every Mellon window, plus Claude Code and Claude
  Desktop: they keep `mellon-mcp.exe` running, and a running exe can't be
  replaced. Then run the newer `Mellon-Setup-<version>.exe`. It installs into the
  same folder and removes the previous version's `_internal\` first. MCP client
  configs keep working because the path does not change.
- **Uninstall:** Windows **Settings → Apps** (Installed apps, or Apps & features
  on Windows 10) **→ Mellon → Uninstall**. The MCP
  registration is separate; remove it with `claude mcp remove mellon` in each
  clone where you added it.

### First launch

1. Start Mellon. The Project Explorer opens empty.
2. **Menu → Open Repository…** (Ctrl+O): pick the **root of your clone**, which
   is the folder that contains `.git` and the document folders. Mellon refuses a
   folder that is not in a git repository, a subfolder of a clone, or a shallow
   clone, and its message says which case it is.
3. Double-click a document to open it in a Document Editor window.

A document that fails to load (for example, one with merge-conflict markers left
in a file) stays in the list. It is greyed out, and its tooltip shows the file,
the line and the error. Fix the file, then press **F5** (Refresh) in the
explorer.

You can also pass the folder on the command line or in a shortcut:
`mellon.exe C:\work\project-reqs`.

### Connect an MCP client

`mellon-mcp.exe` is a stdio MCP server, so the client starts it. Each server
serves one requirements repository, chosen in this order:

1. `--repo-root <path>`, if given;
2. otherwise `CLAUDE_PROJECT_DIR`, which Claude Code sets;
3. otherwise the current directory.

The server exits with status 2 if that folder is not the root of a full git
clone. `--read-only` refuses every write tool, including commits and PDF export.

#### Claude Code

Run this once **in the root of the requirements clone**. It registers the server
for that folder only, in Claude Code's default *local* scope.

cmd:

```
claude mcp add mellon -- "%LOCALAPPDATA%\Programs\Mellon\mellon-mcp.exe"
```

PowerShell:

```
claude mcp add mellon -- "$env:LOCALAPPDATA\Programs\Mellon\mellon-mcp.exe"
```

- No server arguments are needed, because the server uses `CLAUDE_PROJECT_DIR`.
  Start Claude Code in the clone's root, not in a subfolder.
- Repeat the command in every requirements clone you use.
- To check it: `claude mcp get mellon` (or `/mcp` inside a session).
- **Read-only:** remove the entry (`claude mcp remove mellon`), then add it
  again with the flag after the path:
  `claude mcp add mellon -- "%LOCALAPPDATA%\Programs\Mellon\mellon-mcp.exe" --read-only`
- **Don't commit a project `.mcp.json` containing the expanded path.** That path
  points into one person's user profile. Each person registers the server
  locally instead.

#### Claude Desktop

Claude Desktop has no project folder, so use absolute paths and pass
`--repo-root`. Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mellon": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\Mellon\\mellon-mcp.exe",
      "args": ["--repo-root", "C:\\work\\<project>-reqs"]
    }
  }
}
```

- Write full paths with doubled backslashes. Don't rely on `%LOCALAPPDATA%`
  being expanded.
- Add `"--read-only"` to `args` for a read-only server. For each additional
  repository, add another entry with its own name.
- Restart Claude Desktop so it reads the new config. If the server fails to
  start, the reason is in the logs under `%APPDATA%\Claude\logs`.

### Error log

Unexpected GUI errors appear in a dialog that names the log file, and are
appended to:

```
%LOCALAPPDATA%\mellon\mellon\logs\mellon-gui.log
```

If Mellon can't determine that folder, the log goes to
`%TEMP%\logs\mellon-gui.log` instead. The MCP server logs to stderr, and the MCP
client collects that output.

### PDF export

**Export** (Ctrl+E) in a Document Editor works offline. Mellon runs the bundled
`tools\tectonic.exe` with the pre-filled `tools\tectonic-cache`. The export runs
in the background, so the window stays usable while it works. Two environment
variables override this setup: `MELLON_TECTONIC` (a different tectonic binary)
and `TECTONIC_CACHE_DIR` (a different cache, which then needs internet for the
first export). Figures are rendered by draw.io desktop (see [Before you
install](#before-you-install)), which works offline too.

---

## Developer setup (uv, any OS)

Use this setup to work on Mellon itself, or to run it on Linux or macOS. CI
tests on Windows and Ubuntu; macOS is not covered by CI.

### Prerequisites

- **uv**, a recent version (0.11 or newer). It installs Python 3.12 and every
  dependency, so you don't install Python yourself.
  - Windows: `winget install --id=astral-sh.uv -e` (or
    `irm https://astral.sh/uv/install.ps1 | iex`)
  - macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or
    `brew install uv`)
  - Afterwards, open a new shell so `uv` is on `PATH`.
- **git** on `PATH`: the test suite clones its fixture with it. To commit from
  the GUI or MCP server you also need a git identity (`user.name` /
  `user.email`). The tests bring their own identity.
- **Linux only:** Qt's runtime libraries. On Debian/Ubuntu:

  ```
  sudo apt-get install libegl1 libgl1 libxkbcommon0 libxkbcommon-x11-0 \
    libxcb-cursor0 libfontconfig1 libdbus-1-3
  ```

- **tectonic** (optional, only for PDF export): see [PDF export
  (tectonic)](#pdf-export-tectonic).
- **draw.io desktop** (optional, for editing figures and rendering them in
  PDFs): found via `MELLON_DRAWIO`, the standard Windows install folders, or
  `drawio` / `draw.io` on `PATH`. The tests never run it.

### Clone and sync

The repository has an **outer folder** (docs, CI, the test fixture
`req-test.bundle`), and the Python project sits **one level down** in
`mellon/`. Run every `uv` command from that inner folder:

```
git clone <mellon-repo-url> mellon
cd mellon/mellon
uv sync
```

`uv sync` does the following:

- installs CPython 3.12 (pinned in `.python-version`) if it is missing;
- creates `.venv/`;
- installs the exact versions from `uv.lock`, including the dev group.

You never need to activate the venv; `uv run` does that for you.

Dependencies (bounds in `pyproject.toml`, exact versions in `uv.lock`):

| Package | Bound | Used for |
|---|---|---|
| `lxml` | `>=6.1.0,<7` | Reading and writing XML |
| `pygit2` | `>=1.19.2,<2` | Git (libgit2): commits, history, suspect links |
| `jinja2` | `>=3.1,<4` | LaTeX templates for PDF export |
| `mcp` | `>=1.27,<2` | MCP server (FastMCP; mcp 2.x removed it) |
| `pyside6-essentials` | `>=6.11,<7` | GUI (QtCore, QtGui, QtWidgets, QtSvg only) |
| `pytest`, `pytest-qt` (dev) | `>=9.0.3`, `>=4.4` | Tests |

| Command | Purpose |
|---|---|
| `uv sync` | Make `.venv` match `uv.lock` |
| `uv add <pkg>` / `uv add --dev <pkg>` | Add a runtime or dev dependency |
| `uv lock --upgrade` | Move to the newest versions within the bounds |

### Run the tests

```
uv run pytest
```

- The first run clones `req-test.bundle` from the outer folder into the outer
  `req-test/`. If that clone fails (for example, because git is missing), the
  tests that need the fixture skip silently. Set `MELLON_REQUIRE_FIXTURE=1` to
  make a missing fixture an error instead; CI does this.
- On a machine without a display, run
  `QT_QPA_PLATFORM=offscreen uv run pytest`.
- The PDF compile tests skip when tectonic can't be found.
- The full suite takes several minutes.

### Run the apps

```
uv run mellon-gui [path-to-requirements-repo]
uv run mellon-mcp --repo-root <path-to-requirements-repo> [--read-only] [--log-level DEBUG]
```

Both commands accept `--version`. Pass `--repo-root` to `mellon-mcp` when you
run it like this. Otherwise it falls back to `CLAUDE_PROJECT_DIR` and then to
the current directory, which is the source folder, not a requirements repo, so
it is refused.

### Install as a uv tool

This puts `mellon-gui` and `mellon-mcp` on `PATH` as ordinary commands:

```
uv tool install <clone>/mellon              # the inner folder, with pyproject.toml
uv tool install --reinstall <clone>/mellon  # after pulling new source
uv tool uninstall mellon
```

- A tool install resolves dependencies within the `pyproject.toml` bounds; it
  does not use `uv.lock`.
- If the commands are not found, run `uv tool update-shell`.
- To register with Claude Code, run this inside a requirements clone:
  `claude mcp add mellon -- mellon-mcp`.
- PDF export from a tool install needs tectonic on `PATH` or `MELLON_TECTONIC`.

### PDF export (tectonic)

Mellon compiles PDFs with [tectonic](https://github.com/tectonic-typesetting/tectonic/releases).
The installer ships version 0.17.0. Get a prebuilt binary from the releases
page, or run `brew install tectonic` on macOS. Mellon looks for tectonic in this
order:

1. `MELLON_TECTONIC`: the full path to the binary.
2. Frozen (installed) build only: `tools\tectonic.exe` next to the exe.
3. `PATH`.
4. `<project-root>/tools/tectonic[.exe]`, which is `mellon/tools/` in a source
   checkout (gitignored). This does not work from a tool install.

If a `tectonic-cache` folder sits next to the binary, Mellon uses it as
`TECTONIC_CACHE_DIR`, unless you have set that variable yourself. Otherwise
tectonic uses its own per-user cache, and the first export needs internet to
download the TeX files.

### Environments created before 0.2

- 0.2 depends on `pyside6-essentials` instead of the full `pyside6`. If Qt is
  broken after `uv sync` in an older `.venv` (the GUI or the Qt tests fail to
  import PySide6), reinstall the Qt packages, or delete `.venv` and sync again:

  ```
  uv sync --reinstall-package pyside6-essentials --reinstall-package shiboken6
  ```

- Suspect status now follows git's history simplification, the same rule as
  `git log -1 -- <path>`. A link whose ack was recorded on a merge commit shows
  SUSPECT once after upgrading. Re-ack it.

---

## Building the installer

`packaging/build.ps1` builds `Mellon-Setup-<version>.exe` on x64 Windows. It
does not sign anything.

### Prerequisites

- A full clone of the **outer** repository, because the script uses
  `req-test.bundle` from there.
- uv and git on `PATH`.
- Inno Setup 6.3 or later (<https://jrsoftware.org/isinfo.php>); `mellon.iss`
  uses the `x64compatible` architecture identifier, which older 6.x releases
  reject. The script expects
  `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`. If it is somewhere else, pass
  `-Iscc <path to ISCC.exe>`.
- Internet access to PyPI, the tectonic GitHub release and tectonic's TeX bundle
  server.
- The release version set in `pyproject.toml` (`version`). The installer and its
  `AppVersion` take their version from it.

### Run

From the inner `mellon/` folder:

```
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

The installer is written to `build\Mellon-Setup-<version>.exe`, inside the inner
`mellon/` folder. The unpacked app is in `build\dist\Mellon\`, and you can run
it from there for a quick test. Every run deletes and recreates `build\`.

### What the script does

1. Trims `PATH` to the Windows system folders plus the uv and git folders. See
   the next section for why.
2. Exports the locked runtime dependencies (`uv export --frozen --no-dev`) and
   creates `build\venv` with Python 3.12. Installs those dependencies,
   PyInstaller 6.22.3 and then Mellon itself into it.
3. Renders `build\mellon.ico` from the app icon SVG.
4. Runs PyInstaller with `packaging\mellon.spec`. The result is one onedir
   bundle, `build\dist\Mellon`, with two exes that share `_internal\`:
   - `mellon.exe`, the windowed GUI;
   - `mellon-mcp.exe`, the console MCP server.

   The GUI side leaves out the MCP stack and QtNetwork. The MCP side leaves out
   Qt. The software-OpenGL DLL and Qt's translations are dropped; set
   `MELLON_PYI_SLIM=0` to keep them. The script then runs
   `mellon-mcp.exe --version` as a smoke test.
5. Downloads tectonic 0.17.0 (Windows x64 zip) from GitHub, checks its SHA-256
   and unpacks it into `tools\`.
6. Pre-warms `tools\tectonic-cache`: it clones `req-test.bundle` and exports
   every fixture document to PDF with that tectonic, then compiles one line of
   20 pt roman text for the font a page break inside a section heading needs.
   This cache is what lets installed copies export offline.
7. Copies `LICENSE`, `packaging\THIRD-PARTY-NOTICES.txt` and `TEAM.md` into the
   bundle.
8. Runs Inno Setup on `packaging\mellon.iss`. The installer it produces:
   - installs per user to `%LOCALAPPDATA%\Programs\Mellon`, with no admin rights
     and no folder choice;
   - adds a Start-menu entry;
   - removes the old `_internal\` on upgrade;
   - leaves `PATH` unchanged.

   Never change the `AppId` in `mellon.iss`, because upgrades are matched on it.

### Build in CI

The **Installer** workflow (`.github/workflows/installer.yml`) runs the same
script on a GitHub-hosted Windows runner, which ships Inno Setup. It then
installs the result silently and runs `mellon-mcp.exe --version` and
`mellon.exe --version` from the install folder as a smoke test.

- **Actions → Installer → Run workflow** builds from any branch. The installer
  is kept as a run artifact named `Mellon-Setup-<version>`.
- **Pushing a tag `v<version>`** does the same and attaches the installer, plus
  `SHA256SUMS.txt`, to a **draft** GitHub release. Review it and publish it by
  hand. The tag must match `version` in `pyproject.toml` exactly (e.g.
  `v0.3.0`), or the build fails. A version with letters (`rc`, `a`, `b`) is
  marked as a pre-release.

A runner is a clean machine, so CI builds avoid the PATH problem below.

### Build on a clean machine, or at least a clean PATH

PyInstaller hooks copy DLLs they find on `PATH` into the bundle. In one build, a
foreign OpenSSL DLL (from a password manager) replaced CPython's own in
`_internal\`. That is why the script trims `PATH` and the spec excludes
QtNetwork. The uv and git folders stay on `PATH`, so keep unrelated DLLs out of
them. Prefer a clean machine or VM for release builds.

### Before distributing

- Test on a clean Windows profile or VM:
  1. Install without admin rights.
  2. Open a repository.
  3. Export a PDF with the network off.
  4. Register `mellon-mcp.exe` with Claude Code and call a tool.
- The exes and the installer are **unsigned**; code signing is not part of the
  build. Distribute from an intranet share: that usually avoids SmartScreen,
  though Windows may still show an "Unknown Publisher" warning if the share
  isn't in the Local intranet zone. Ask IT whether AppLocker, WDAC or Smart
  App Control blocks unsigned apps on team machines.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Could not open repository: … is not inside a git repository" | Open the root of a git clone (the folder that contains `.git`), or clone the repository with your git client first. |
| "… is not the root of its git work tree (…)" | You picked a subfolder, such as a single document folder. Open the folder named in the message. |
| "… is a shallow clone" | Run `git fetch --unshallow` in the clone; suspect-link detection needs the full history. CI jobs need `fetch-depth: 0`. |
| "… cannot open git repository: …" about ownership | Git refuses repositories it doesn't consider yours. This is common on network drives. Clone to a local disk under your user. |
| Commit fails: "git user.name and user.email are not configured" | Run the two `git config --global` commands from [Before you install](#before-you-install). |
| Commit fails: "a merge (or another git operation) is in progress" | Finish (or abort) the merge in your git client, then commit in Mellon. |
| A document is greyed out in the Project Explorer | It failed to load. The tooltip shows the file, line and message (often leftover conflict markers). Fix the file, then press F5. |
| Amber **Changed on disk** banner, and edits are refused | The files changed under the window (a pull, another window, the MCP server). Press F5 to reload. |
| Claude Code shows `mellon` as failed | Usually Claude Code was started outside the clone's root, or the clone is shallow. To see the reason, run the server by hand from the clone's root (cmd): `"%LOCALAPPDATA%\Programs\Mellon\mellon-mcp.exe" --repo-root .`. It either prints the problem and exits, or logs `mellon-mcp serving …` and waits (press Ctrl+C). |
| Setup can't replace files, or asks you to close applications | `mellon.exe` or `mellon-mcp.exe` is still running. Close Mellon, Claude Code and Claude Desktop, then run Setup again. |
| PDF export: "tectonic binary not found" | Happens in developer and uv tool installs only. Install tectonic, or set `MELLON_TECTONIC` (see [PDF export (tectonic)](#pdf-export-tectonic)). |
| PDF export: "The TeX support files aren't cached yet and couldn't be downloaded" | The cache in use is empty. Connect to the internet for one export. |
| Linux: `Could not load the Qt platform plugin "xcb"` | Install the Qt runtime libraries from [Prerequisites](#prerequisites). |
| `uv sync` in the outer folder says there is no `pyproject.toml` | `cd mellon` first; the Python project is one level down. |
| `uv` not found right after installing it | Open a new shell. |

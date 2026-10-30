# Mellon — orientation for Claude

A DOORS-like requirements management tool. Requirements live as XML files in a git repo, so versioning, branching, diffs, and review come for free. The library, MCP server and PySide6 GUI are all feature-complete, and a frozen Windows installer is built from `mellon/packaging/`. v0.3.0 is the current release; see `mellon/CHANGELOG.md`.

**The Python package is one directory deep**, in `mellon/`. Run all `uv` commands from there, not from this outer directory.

> Note: the outer working directory and the Python package share the name `mellon`, so the package source sits one level down at `mellon/mellon/`. (The project was formerly named `req-tool` / `reqtool`; you may still see that name in older memories or git history.)

## Repo layout

```
./                              <-- you are here (outer working dir, repo root)
├── CLAUDE.md                   <-- this file
├── README.md                   overview, install, quickstart
├── LICENSE                     copy of mellon/LICENSE, so GitHub detects the license
├── .github/workflows/
│   ├── ci.yml                  CI: ubuntu + windows tests, wheel smoke job
│   └── installer.yml           Windows installer build (manual, or v* tag → draft release)
├── mellon/                     the actual Python package
│   ├── README.md               short entry point
│   ├── INSTALL.md              installer, MCP client config, dev env setup (uv, Python 3.12)
│   ├── USAGE.md                on-disk format, schema rules, suspect workflow
│   ├── STATUS.md               authoritative current state + file map
│   ├── CHANGELOG.md            per-release changes (Keep a Changelog)
│   ├── TEAM.md                 team routine: setup, pull/F5/edit/commit/push, conflicts
│   ├── LICENSE                 GPL-3.0-or-later (packaged into the wheel and installer)
│   ├── pyproject.toml / uv.lock
│   ├── src/mellon/             source
│   ├── tests/                  pytest suite
│   ├── packaging/              Windows installer: build.ps1, mellon.spec, mellon.iss, notices
│   └── tools/                  (gitignored) optional vendored tectonic for source checkouts
├── req-test.bundle             test fixture (committed)
└── req-test/                   unpacked fixture (.gitignored, auto-cloned)
```

## Read these before exploring code

- `mellon/STATUS.md` — what's built, what's deferred, full file map. Read this first.
- `mellon/CHANGELOG.md` — what changed per release, including the 0.2.0 upgrade notes.
- `mellon/USAGE.md` — on-disk format, schema/folder rules, validation rules, suspect-link workflow.
- `mellon/TEAM.md` — how a team uses Mellon with an external git client.
- `mellon/INSTALL.md` — env setup if anything's broken.

## Common commands

All run from inside `mellon/`:

| Command | Purpose |
|---|---|
| `uv sync` | Install/update env from `uv.lock` |
| `uv run pytest` | Run the full suite (~1200 tests; clones fixture on first run; set `QT_QPA_PLATFORM=offscreen` for headless GUI tests) |
| `uv run mellon-mcp [--repo-root <path>] [--read-only]` | MCP stdio server (45 tools). The root defaults to `$CLAUDE_PROJECT_DIR`, then the cwd, and must be the root of a full git clone (otherwise exit 2) |
| `uv run mellon-gui [path]` | PySide6 GUI (Project Explorer + per-doc Document Editor windows) |
| `powershell -ExecutionPolicy Bypass -File packaging\build.ps1` | Build `build\Mellon-Setup-<version>.exe` (needs uv, git, Inno Setup 6.3+, internet) |

## Module map (`mellon/src/mellon/`)

| File | What lives there |
|---|---|
| `schema.py` | Frozen dataclasses: `Document`, `Metadata`, `Attribute`, `OutlineNode`, `RequirementObject`, `Link`, `Table`/`TableRow`/`TableColumn`/`TableCell`; `table_geometry_errors` (shared tiling check) |
| `loader.py` | XML → `Document` |
| `validator.py` | Structural checks |
| `editor.py` | **Every mutation lives here** — single source of truth for create/edit/delete/move/link. Atomic LF writes, text sanitiser, `_child_path` path guard |
| `suspects.py` | `scan_document`, `scan_repo`, `ack_all` |
| `trace.py` | Recursive up/down traversal, cycle-safe |
| `repo.py` | Multi-doc discovery, `load_all` + `DocLoadError` (failed docs are reported, not dropped), cross-doc incoming links, `folders_fingerprint`/`doc_folder_names` staleness helpers |
| `git_ops.py` | `pygit2` wrappers (`check_repo_root`, `last_commit_for_paths`, `commit`, `discard_paths`, `show_blob_at`, `log_for_path`, `head_summary`, `dirty_count`, `current_issue`, `create_issue_tag`) |
| `filter.py` | Saved-filter engine: `evaluate(doc, filt)`, operators per attribute kind |
| `coverage.py` | `coverage_report` (direct + transitive, headings-aware) |
| `search.py` | `search_objects`: case-insensitive substring scan with ranked hits |
| `diff.py` | `compare_document(repo_root, doc_id, ref_a, ref_b)`: structured diff between two refs. Library only; there is no Compare dialog yet |
| `merge.py` | Resolve a git merge that stopped with conflicts in documents: `pending`, `plan`, `resolve`. Renumbers the later of two objects created under one id (with their links), merges objects per field, link files as sets and outlines by position; only same-field conflicts go to the user (with trace context). GUI: Project Explorer banner + `MergeDialog` |
| `export.py` | LaTeX/PDF export via Jinja2 + tectonic (`templates/syrs/`); tectonic lookup `MELLON_TECTONIC` → `<exe-dir>/tools` → PATH → `<project-root>/tools`. Figures are rendered by draw.io (`export.pdf(doc, folder=…)`) |
| `drawio.py` | draw.io desktop integration (stdlib + lxml only — the frozen MCP exe has no Qt): `find_drawio` (`MELLON_DRAWIO` → Windows install dirs → PATH), `is_drawio_svg`, `BLANK_DRAWIO_SVG`, `open_in_drawio`, `qt_svg` (QtSvg-friendly rewrite: `light-dark()` colours, HTML labels), `export_pdfs` (one batch CLI run → cropped vector PDFs) |
| `mcp/server.py` | FastMCP stdio server, 45 tools (13 read / 27 write / 5 git+review) |
| `mcp/serialize.py` | dataclass → JSON-serializable dicts |
| `service/` | Cache (incl. load errors, HEAD-keyed suspect scans), document service, error/result types shared by GUI + MCP |
| `gui/app.py` | `QApplication` entrypoint; installs the excepthook (log at `%LOCALAPPDATA%\mellon\mellon\logs\mellon-gui.log`) and opens a `ProjectExplorerWindow` |
| `gui/project_explorer_window.py` | Top-level window: list of docs + open editors. Double-click opens a `DocumentEditorWindow` |
| `gui/main_window.py` | `DocumentEditorWindow` — three-pane editor for a single document |
| `gui/controller.py` | `RepoController` (load, selection, Qt signals) |
| `gui/theme.py` | `ThemeManager` singleton (dark/light tokens, `QPalette` + QSS) and `build_theme_menu` helper shared by both windows |
| `gui/delegates.py` | Per-column grid delegates: state gutter, id, multi-line description (+ table preview rendered by `InlineTableWidget`), attribute-kind aware editor |
| `gui/models/` | `ProjectTreeModel`, `OutlineModel`, `ObjectsGridModel` |
| `gui/widgets/` | `InlineTableWidget` — in-place editable table grid (cell-text editing), opened as the description-column editor for table objects; structural controls (add/delete row+col, merge/split, header, alignment, insert/remove) live in the toolbar **Table** dropdown, which drives the live editor |
| `gui/panels/` | `OutlinePane` (left), `GridPane` (center), `RightPane` tabs (`Inspector` + `History`) |
| `gui/dialogs/` | `LinkPicker`, `TraceView`, `CommitDialog`, `DocumentPropertiesDialog`, filter + view manager dialogs |

Outside `src/`, **`mellon/packaging/`** holds the Windows installer:
- `build.ps1` builds it with PyInstaller onedir and Inno Setup.
- `mellon.spec` defines `mellon.exe` (GUI, windowed) and `mellon-mcp.exe` (console).
- `mellon.iss` is the Inno Setup script (per-user install to `%LOCALAPPDATA%\Programs\Mellon`).
- `launch_gui.py` / `launch_mcp.py` are the entry scripts.
- `THIRD-PARTY-NOTICES.txt` ships with the installer.

The installer bundles tectonic 0.17.0 and a pre-warmed TeX cache.

Tests mirror the layout: one `tests/test_<module>.py` per source module. `tests/conftest.py` auto-clones `req-test.bundle` into `../req-test/` on first run.

## Conventions

- **DOORS terminology.** When naming object types, attributes, statuses, link types, or other domain vocabulary, use IBM DOORS conventions over generic alternatives (e.g. `Information` over `Text`, `Heading` over `Section`). The user's mental model is DOORS-shaped.
- **Editor mutates, caller commits.** Every function in `editor.py` writes files but never commits. The GUI commits via an explicit dialog; the MCP server has a separate explicit `commit_changes` tool.
- **Two-commit suspect-link rule is intentional.** A file cannot record its own commit's hash, so revising a parent and re-acking children always takes two commits. This is the design — don't try to "fix" it.
- **One test file per source module.** Match the existing pattern when adding tests.
- **Folder name = manifest id.** A document's folder name must match its manifest's `id` attribute, and an object file's stem must match its `<object id>`.
- **Outgoing links only on disk.** `links/<id>.xml` files hold *outgoing* links from object `<id>`. Incoming links are computed by scanning the whole repo.
- **Canonical 2-space XML indentation** is applied on write via `lxml.etree.indent`. First edit of a hand-authored file may collapse decorative blank lines — expected, one-time.

## Fixture

The `req-test/` directory is a separate git repo committed as `req-test.bundle`. Tests find its commits by message through the `fixture_commit` fixture in `tests/conftest.py`, never by hash, so rewriting that history only means keeping the commit messages and re-pointing the `ack="<hash>"` values in its link files. To refresh after editing the fixture:

```
git -C req-test bundle create ../req-test.bundle --all
```

`conftest.py` only clones the bundle when `req-test/` is missing, so delete `req-test/` after pulling a new bundle.

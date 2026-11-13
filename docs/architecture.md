# Architecture

How Mellon is built: its parts, the interfaces between them and to the outside, how data moves, its modes, and what it relies on. Requirements say what Mellon does and survive a rewrite; this page says how the current code does it and changes with the code. [failure-analysis.md](failure-analysis.md) is checked against it.

- **Source.** No architecture description existed, so this one was written from the code's structure (`mellon/src/mellon/`) and the docs (`mellon/STATUS.md`, `USAGE.md`, `SCALING.md`, `TEAM.md`, `INSTALL.md`). It describes v0.3.0 plus the Unreleased changes in `mellon/CHANGELOG.md`.
- **Scope.** It covers the engineering framework's architecture checklist items A1-A7, A9, A10 and A13-A15, and the level (H4). Timing (A8) and hardware (A11, A12) don't apply to a desktop tool, except as noted under [Resource budgets](#resource-budgets-a10).
- **Values** that nobody has measured or decided are written `TBD` and listed under [Open points](#open-points).

## Level (H4)

Mellon is one software item at level `High`, and so is every part of it.

- The GUI, the MCP server, `mellon-check` and the library are one Python package and run the same code. Nothing separates one part from another: they share a process, memory, caches and the files on disk. So no part can be shown not to interfere with another, and none can take a lower level.
- The highest effect is a wrong answer that looks right (a link shown OK, a document shown valid or covered) or a silent change to stored requirements. Reviews and release gates trust those answers. [specification.md](specification.md#level) records the same decision.

## Context

```
  user ──── GUI (mellon-gui / mellon.exe) ───┐
  agent ─── MCP server (mellon-mcp), stdio ──┤
  CI ────── mellon-check ────────────────────┼── DocumentService ── library ──┬── files in the work tree
  scripts ─ library (import mellon) ─────────┘                                ├── git repository (pygit2)
                                                                               ├── draw.io desktop (subprocess)
                                                                               └── tectonic (subprocess)
  external git client ── same work tree and .git (pull, push, merge, commit)
```

Mellon commits but never pulls, pushes or merges. Those happen in the user's own git client, on the same work tree, often while Mellon is running.

## Components (A1)

Each component exists for a feature area of the specification ([specification.md](specification.md#documents)). Most areas have no requirements yet, so allocation is by area; a component with no area is marked as a design decision.

| Component | Modules | Why it exists | Areas |
|---|---|---|---|
| Model | `schema.py` | Immutable in-memory form of a document (frozen dataclasses), shared by every other part. The table tiling check lives here so loader, editor and validator agree. | STORE |
| Loader | `loader.py`, `repo.py` | Reads the on-disk format into the model; finds documents (top-level folder `X` with `X/X.xml`, dot-folders skipped); reports a failed document as a `DocLoadError` instead of dropping it. Keeps parsed files while their modified time and size are unchanged. | STORE |
| Editor | `editor.py` | Every edit to document files: atomic per-file writes, the write journal (all-or-nothing operations, undo data), the text sanitiser, the path guard (`_child_path`), object id allocation. Writes, never commits. | STORE, EDIT, TRACE |
| Git access | `git_ops.py` | Wraps pygit2: root check, commit, discard, status, history, blob reads, issue and free tags. | STORE, VER |
| Link index | `link_index.py` | Last commit of every object of a document in one history walk, kept per commit in memory and in `.git/mellon-link-index.json`. Exists for speed (design decision, see `SCALING.md`); it must give the same answer as `git_ops.last_commit_for_paths`. | TRACE |
| Suspects | `suspects.py` | Classifies each outgoing link OK, SUSPECT or BROKEN; writes acks. | TRACE |
| Trace, coverage | `trace.py`, `coverage.py` | Up and down traversal, cycle-safe; coverage direct and transitive, headings excluded by default. | TRACE |
| Search, filter | `search.py`, `filter.py` | Substring search; saved-filter engine. | EDIT |
| Validator | `validator.py` | Structural checks and the rules engine, with stable error codes. | CHECK |
| Compare | `diff.py` | Changes in one document between two versions (refs or the working copy). | VER |
| Merge | `merge.py` | Resolves a git merge that stopped with conflicts in documents: renumbers clashing ids, merges per field, writes the result, marks it resolved. | VER |
| Export | `export.py`, `templates/syrs/`, `drawio.py` | LaTeX via Jinja2, compiled by tectonic; figures rendered by draw.io. `drawio.py` uses only the standard library and lxml, so the frozen MCP server (built without Qt) can use it. | PDF, EDIT (figures) |
| Service | `service/document.py`, `cache.py`, `errors.py`, `results.py` | The one façade the GUI, MCP server and `mellon-check` call. Maps library errors to `ServiceError` + `ErrorCode`, enforces read-only, keeps the optional cache (GUI only), records each write call's files for undo. | all |
| MCP server | `mcp/server.py`, `mcp/serialize.py` | 46 tools over stdio (13 read, 27 write, 6 git/review/export); `--read-only`; optional auto-commit. | MCP, plus each tool's feature area |
| GUI | `gui/` | Project Explorer, Document Editor windows, dialogs, theme, settings, error log. | GUI, plus each feature's area |
| Gate | `check.py` | `mellon-check`: exit 0, 1 or 2 over load errors, validation and link states, computed from scratch. | CHECK |
| Packaging | `mellon/packaging/` | Windows installer: PyInstaller onedir, tectonic with a pre-filled TeX cache, Inno Setup. | GUI (installation) |

**Layering.** Adapters (GUI, MCP, `check.py`) call `DocumentService`; the service calls the library; the library never imports the service, Qt or MCP. The exceptions are listed under [Size and coupling](#size-and-coupling-a5).

**Writes outside `editor.py`.** CLAUDE.md says every mutation lives in `editor.py`. These write repository files without it:

| Writer | Files | Journaled (rollback, undo) |
|---|---|---|
| `merge.resolve` | document files of a merge in progress, and the index | no |
| `git_ops.discard_paths` | a document folder, and the index | no |
| `git_ops.commit`, tags | index, refs, objects | no (git's own) |
| `suspects.write_ack` | `links/<id>.xml` | yes, through `editor._write_tree` |
| GUI commit dialog | manifest put back after a failed commit (`main_window.py`) | no; an atomic write through `Journal.restore` |
| `link_index._save` | `.git/mellon-link-index.json` | no (a cache) |

## Interfaces (A2)

| Interface | Data | Owner | Failure behaviour |
|---|---|---|---|
| **Document files** (work tree) | Manifest, objects, links, figures, views, filters, rules; UTF-8 XML with LF, 2-space indent; format in `USAGE.md`. | Mellon owns the format. Mellon, the user's git client, draw.io and hand edits all write the files. | A file that doesn't parse makes its document a load error (file, line, message, conflict-marker hint). Writes go to `.<name>.<pid>.tmp` then `os.replace`, retried 5 times at 50 ms when Windows reports the file busy; there is no flush to disk (`fsync`). A multi-file operation is rolled back from an in-memory journal if it raises. |
| **Git repository** via pygit2 | History, trees and blobs (suspect hashes, compare, history, merge), status (dirty files), index, commits, tags, merge state. | The user's git repository. Mellon reads all of it and writes commits, index entries and tags; the external git client does the rest. | Refusals raise `GitError` → `ServiceError(GIT)`: merge or other operation in progress, `index.lock` present, no identity, tip moved during a commit. A refused commit commits nothing; paths may be left staged. |
| **Link index** `.git/mellon-link-index.json` | `{version: 1, docs: {doc: {commit, hashes: {obj_id: short hash or null}}}}` | Mellon (cache). Each process keeps its own copy in memory. | Unreadable or wrong-version file is ignored (costs a history walk). Written after a walk of 50 commits or more, atomically; a failed write is ignored. Entries that parse are trusted without a check. `mellon-check` ignores the file. |
| **MCP** (stdio, JSON-RPC) | 46 tools; results `{ok, affected_files, message, error?, error_type?}` for writes; read shapes in `STATUS.md`. | Mellon defines the tools; the agent's client starts the server. | Root refused at start → exit 2. A missing document is a JSON-RPC error; other failures are `ok: false` with a stable `error_type`. Read-only is enforced at each write tool and again in the service. Tools run one at a time on the event loop (see [Concurrency](#concurrency-and-shared-resources-a9)). |
| **GUI** | Windows, dialogs, banners, status bar. | Mellon. | Expected failures show in the status bar for a few seconds, or in a dialog. Uncaught exceptions go to the error log and a rate-limited dialog (one per 10 s). |
| **`mellon-check`** CLI | `--repo-root`; one line per problem on stdout; exit 0, 1, 2. | Mellon. | Root refused → exit 2. `ServiceError` → exit 2. A repo with no documents exits 0. |
| **Library API** | Names exported from `mellon/__init__.py` (table in `USAGE.md`). | Mellon. Used by tests, benchmarks and scripts. | Raises `EditorError`, `LoaderError`, `GitError`. Not versioned separately. |
| **draw.io desktop** | GUI: opens `figures/<id>.svg` detached; draw.io saves in place. Export: one batch CLI run to PDFs in a temp folder. | The user installs it; any version. | Not found → figures can't be edited and print as placeholders, listed in `unrendered`. Export run limited to 300 s. Version not checked. |
| **tectonic** | LaTeX in, PDF out, in a temp folder. | Bundled 0.17.0 in the installer (SHA-256 checked at build); otherwise the user's. | Not found or not runnable → `TectonicError`. Limited to 300 s; the timeout message suggests a network problem even when the document is just large (`SCALING.md`). |
| **QSettings** | Theme, window geometry, splitter and pane sizes, active view, column widths and order, hidden columns, active filter, last export folder. | GUI. Per user: registry on Windows (`HKCU\Software\mellon\mellon`). | Keyed by document id only, not by repository. A missing or bad value falls back to a default. |
| **Environment and options** | See [Configuration and variants](#configuration-and-variants-a13). | User, client, installer. | See there. |
| **Error log and stderr** | GUI: `%LOCALAPPDATA%\mellon\mellon\logs\mellon-gui.log`, temp folder fallback. MCP: stderr. | Mellon. | A failed log write is ignored. |

## Data and control flow (A3)

Each flow from an input to its outputs. "Service" is `DocumentService`.

| Flow | Path |
|---|---|
| **Open a repository** (GUI) | `open_repo` → `git_ops.check_repo_root` (git root, not shallow, openable) → service with cache → `load_all` → `discover_documents` → `load_document` per folder (parse cache) → `RepoCache.put_all` with load errors → Project Explorer list. Refused: the previous repository stays open. |
| **Field edit** (GUI grid cell or Inspector) | Window checks read-only and the stale flag, writes pending text → service method in a `_recorded` journal → editor reads the file from disk, changes one element, writes it → service re-reads the document (suspect scans kept) → window's undo step from the journal → `object_changed` → panes repaint one row → folder fingerprint retaken. |
| **Structural edit** (new, delete, move, link) | As above, but the editor also rewrites the manifest outline; delete also strips incoming links in other documents' `links/` files after checking that every document loads. The service drops the cached copies of every touched document; the grid model is reset. |
| **MCP write** | Tool → `_guard_write` → `_require_doc` → service method (no cache; journaled per editor call) → result with `affected_files` → if `commit_message` is set, `service.commit(paths=affected_files)` with the `Via: mellon-mcp` trailer; a failed commit adds `commit_error` and leaves the write on disk. No validation. |
| **GUI commit** | `Ctrl+S` → read-only/stale check → `validate_document` on the **cached** copy; errors block → `prepare_document_commit` → commit dialog → author/date written to the manifest → `commit_document` lists the folder's dirty paths **again from disk** → `git_ops.commit` (tree = HEAD + those paths; refused if HEAD moved) → issue tag and free tag at the HEAD read **after** the commit → window back to read-only, undo cleared. A failed commit puts the manifest back unless HEAD moved and the latest commit holds the manifest as written. |
| **MCP commit** | `commit_changes(message, paths?)` → service → `git_ops.commit` with every uncommitted path inside document folders (or the given paths). No validation. |
| **Suspect scan** | Service `scan_suspects(doc)`; in the GUI cached per document and keyed by HEAD → `scan_document`: lists each target document's `objects/` on disk (missing → BROKEN) → `link_index.current_hashes(repo, target_doc)` (no commit → SUSPECT) (memory → disk file → walk from HEAD to the remembered commit) → compare with `ack`. The GUI's gutter scans every document in the repo to get incoming states. |
| **Acknowledge** | GUI Inspector or "Ack selected", or MCP `ack_suspects` → statuses from a scan **at ack time** → `suspects.ack_all` writes each link's `ack` = its `current_hash`, journaled. The ack records the latest commit, not the commit the reviewer looked at. |
| **Validate** | Service `validate_document` / `validate_repo` → `validator.validate` on the loaded (GUI: cached) model. |
| **Trace, incoming, coverage, search** | Service → `get_all_documents()` (GUI: cached snapshot of every document) → library function. Documents that fail to load are left out without a mark. |
| **Compare** | GUI: `CompareDialog` → thread pool → `diff.compare_document` with its own repo handle; blob ids compared before parsing; working copy read from disk; results matched to the request number on the GUI thread. MCP: the same call, synchronous. |
| **Merge resolve** | Explorer activation or F5 → `merge.pending` → banner → `merge.plan` (base, HEAD, MERGE_HEAD trees + index conflicts) → `MergeDialog` → `merge.resolve` writes every document file both sides changed (whatever is in the work tree) and updates the index → user commits in the git client → F5. |
| **Discard** | Menu → confirm → `discard_paths(folder)`: refuses during a merge or with `index.lock`; plans from `repo.status()`; writes the index, then restores or deletes files → cache dropped, document reloaded. |
| **Undo / redo** | `Ctrl+Z` → pending text written → `service.undo_changes(journal)` → refused if any file differs from the step's after-bytes; otherwise before-bytes written → cache dropped → reload. |
| **External change** | Window activation, each write through the window's service, and the commit (before and after the Commit dialog) → content digests of the document's folder and its parents' folders (cached per path by mtime and size, re-read within 2 s of the mtime; editor side files left out) → differ → "Changed on disk" banner, the write refused until F5. The window's own writes move the digests on. A figure rewritten in place is let through on activation. HEAD moved → link states re-read. Undo and redo check their own step's files instead. |
| **Export** | GUI: `Ctrl+E` → pending text written → document and parents snapshotted → link scan → warning if suspect or broken → save dialog → `_ExportJob` on the thread pool: Jinja2 → draw.io batch (300 s) → tectonic (300 s) in temp folders → PDF bytes written to the target. The document as on disk, uncommitted edits included, labelled with the current issue. MCP: same, synchronous, suspect links returned, not blocking. |
| **`mellon-check`** | Root check → `link_index.from_scratch` → read-only service → `validate_repo` (collects load errors) → `scan_suspects(None)` → print → exit code. |

## Modes (A6)

| Mode | Entered | Left | What changes |
|---|---|---|---|
| GUI, no repository | Start without a path; a failed load | Open Repository | Only the Explorer. |
| Root refused | `check_repo_root` fails (not git, not the work-tree root, shallow, unopenable) | Open another folder | GUI: message, previous repository stays. MCP and `mellon-check`: exit 2. |
| Document Editor read-only | Every window opens in it; after a successful commit | Menu → Read-only, or **Enable editing** | Edit actions disabled per widget. Compare, export, trace, history work. The GUI's service itself stays writable. |
| Document Editor editing | Enable editing | Commit succeeds, or Read-only | Edits write to disk at once; undo steps recorded. |
| Changed on disk (stale) | Activation, a write or a commit finds a file's content changed | F5 | Edits, undo and commit refused; pending text kept in its editor. |
| Document fails to load | A file doesn't parse or breaks a naming rule | Fix the file, F5 | Explorer: greyed, can't open. Open editor: last good copy behind the stale banner. Repo-wide: delete refused; MCP `suspects()` without `doc_id` refused; trace, incoming, coverage and search silently leave it out. `mellon-check`: exit 1. |
| Merge in progress | The user's git client stops a merge with conflicts | Merge commit or abort in the git client | Explorer banner and **Resolve merge…**; commits and discard refused. Editing is **not** refused. |
| Index locked | Another git program holds `index.lock` | It finishes | Commit and discard refused with "commit again" / "discard again". |
| Detached HEAD | The user's git client | Checkout of a branch | Status bar tooltip. Commits are allowed and land on the detached HEAD. |
| Unborn HEAD (no commits) | New repository | First commit | Every link to an object on disk SUSPECT ("never reviewed") and not ackable; links to missing objects BROKEN; discard refused. |
| Exporting | Export started | Job ends | Export action disabled in that window; quitting waits for the job. |
| MCP read-write / read-only | `--read-only` at start | Restart | Read-only refuses the 27 write tools, `commit_changes` and `export_document`. Fixed for the process. |
| draw.io or tectonic missing | Not found at use | Installed | Figure editing off and placeholders in the PDF; export fails without tectonic. |

## Startup, shutdown and crashes (A7)

**Startup order.**

- **GUI:** parse arguments → `QApplication` and its names → error log hook → font, theme (from QSettings) → Project Explorer → if a path was given, `open_repo` (root check, load every document). Nothing else is assumed ready; draw.io and tectonic are looked up when used.
- **MCP:** parse arguments → logging to stderr → root (`--repo-root`, else `CLAUDE_PROJECT_DIR`, else the current directory) → `check_repo_root` → `discover_documents` (an empty repo only warns) → service → serve. The root is not re-checked later.
- **`mellon-check`:** root, root check, then the check from scratch.

**Shutdown.** The GUI waits for running pool jobs (export, compare) after the event loop ends, so a PDF being written isn't cut off. Window geometry and pane sizes are saved on close. The MCP server ends when its client closes stdin.

**What survives a crash or power loss.**

| State | Kept on disk | Lost |
|---|---|---|
| One file being written | Old or new content: `os.replace` is atomic. A stray `.<name>.<pid>.tmp` may stay; it isn't a side file, so it is listed as uncommitted and a commit would include it. | After power loss, without a flush the new file may be empty or partly written: `TBD`, not measured. The committed copy is in git. |
| An operation across several files | The files written so far. | The rollback journal is in memory. A kill mid-operation leaves it partly applied, for example incoming links stripped from other documents while the deleted object remains. |
| Undo history | — | All of it (in memory). |
| Commit | Index written before the commit object: a crash between leaves the paths staged, not committed. | — |
| Merge resolve | Files written so far. | Index update (written last); re-running finishes the job. |
| Discard | Index written first. | Re-running finishes the job. |
| Exported PDF | — | The target is written with a plain write: a crash leaves a truncated file. |
| Link index | Previous file (atomic replace). | Latest walk; costs a walk next time. |
| QSettings | Qt's own persistence. | — |
| GUI error log | Lines already appended. | — |

## Concurrency and shared resources (A9)

**Inside a GUI process.** One main thread does all model, cache and file work. A global `QThreadPool` runs two kinds of job:

- **Export** gets an immutable snapshot of the document and its parents, then reads figures from disk and runs draw.io and tectonic.
- **Compare** opens its own pygit2 repository and parses bytes without the loader's parse cache.

Neither touches `RepoCache` or the loader's cache. `link_index` keeps its memory under a lock. All Document Editor windows of one repository share **one** `DocumentService` and `RepoCache`; each window has its own controller, fingerprint and undo history.

**Inside the MCP server.** FastMCP 1.x calls the synchronous tool functions on its event loop, so tools run one at a time. A long `export_document` (up to about 600 s with draw.io and tectonic) blocks every other call.

**Between processes.** A GUI, one or more MCP servers (one per client session), an external git client and draw.io work on the same work tree and `.git`. There are no Mellon locks.

| Shared resource | How it is accessed | Protection |
|---|---|---|
| Document files | Read-modify-write per file by each process. | Atomic replace per file. Nothing stops two writers from losing each other's change to the same file (for example the manifest outline). The GUI notices a change only on window activation; the MCP server re-reads every call. The team routine asks for one writer per document. |
| git index | libgit2 and git. | `index.lock` checked before commit and discard. |
| Branch ref | Commits from several processes. | `create_commit` with the HEAD read at the start is refused if the tip moved. Tags are created at the HEAD read afterwards. |
| Tags | Created by Mellon and git clients. | Creating an existing name is refused. |
| `.git/mellon-link-index.json` | Read once per process, rewritten after long walks. | Atomic replace; last writer wins, which only costs a walk. |
| Loader parse cache | Process memory, keyed by path, mtime and size. | Files changed in the last 2 s aren't kept, as in git. |
| libgit2 object cache | Process-wide, 256 MB by default. | None; see `SCALING.md` on the cliff it caused. |

## Resource budgets (A10)

From `SCALING.md` (one Windows machine, antivirus on, often under load; synthetic linear history). "Target" values are estimates set before the work, not requirements.

| Budget | Measured | Limit or target | Margin |
|---|---|---|---|
| Objects per document | — | 1,000 recommended; not enforced | — |
| PDF export | 101 s at 1,000; 191 s at 2,000; times out at 5,000 | tectonic 300 s, draw.io 300 s | about 3× at 1,000 on that machine |
| Suspect scan, first ever | 4-13 s at 5,000 | ≤ 10 s | not met under load |
| Link states after one commit | 0.05-0.15 s at 5,000 | ≤ 0.2 s | met |
| Open window (index on disk) | 5.3-6.4 s at 5,000 | ≤ 5 s | not met |
| Edit one cell | 0.41-0.49 s at 5,000 | ≤ 0.5 s | met, narrowly |
| F5 reload / new object | 3.5-4.6 s at 5,000 | ≤ 3 s | not met |
| Compare, worst case | 0.19 s at 1,000; 1.0 s at 5,000 | 2 s at 1,000 (MELLON-VER-SRS-57) | about 10× |
| First read after a fresh clone | 21 s at 500; about 80-97 s at 2,000 | — | — |
| Has-incoming filter | 5-14 s at 5,000 | — | — |
| MCP `outline`, whole document | 1 MB of JSON at 5,000 | — | `root` and `depth` added |
| Memory | `TBD`: not established | `TBD` | — |
| Repositories with many documents | `TBD`: not benchmarked | — | — |
| Link index file size | `TBD` | — | — |
| GUI error log size | grows without limit | `TBD` | — |

Write endurance, power and heat don't apply. Measuring: `benchmarks/run_all.py`.

## Configuration and variants (A13)

**Variants.**

| Variant | Contents | Differences |
|---|---|---|
| Windows installer | `mellon.exe` (windowed GUI), `mellon-mcp.exe` (console, no Qt), tectonic 0.17.0 with a TeX cache, per-user install | No `mellon-check`. Exact locked dependencies. GUI has no console: `--version` only from `mellon-mcp.exe`. |
| Source checkout (`uv sync`, `uv run`) | All three commands | Locked dependencies. tectonic from `MELLON_TECTONIC` or `PATH`, never from inside the repository. |
| `uv tool install` | All three commands on `PATH` | Resolves dependencies within the `pyproject.toml` bounds, **not** `uv.lock`, so versions can differ from those tested. CI has a wheel smoke job for this. |
| Platforms | Windows (installer, CI), Ubuntu (CI) | macOS: `TBD`, not in CI. |

**Settings.**

| Name | Where | Valid values | If wrong or missing |
|---|---|---|---|
| `--repo-root` | MCP, check | Root of a full git clone | Exit 2. |
| `CLAUDE_PROJECT_DIR` | MCP, check | As above; set by Claude Code | Falls back to the current directory; a wrong folder that is still a valid root is served without warning. |
| `--read-only` | MCP | flag | — |
| `--log-level` | MCP | DEBUG, INFO, WARNING, ERROR | argparse refuses. |
| `MELLON_TECTONIC` | export | path to a tectonic binary | Used as is; a bad path fails at export. |
| `TECTONIC_CACHE_DIR` | export | folder | Overrides the bundled cache; an empty cache needs internet. |
| `MELLON_DRAWIO` | figures, export | path to draw.io | Used as is; a bad path fails at use. |
| `ProgramFiles`, `LOCALAPPDATA` | draw.io lookup | Windows folders | Skipped if unset. |
| git `user.name`, `user.email` | commits, tags | non-empty | Commit refused with the `git config` commands. |
| QSettings keys | GUI | see [Interfaces](#interfaces-a2) | Default used. Keyed by document id, so two repositories with the same document id share them. |
| `QT_QPA_PLATFORM`, `MELLON_REQUIRE_FIXTURE` | tests | `offscreen`; `1` | Tests fail or skip. |
| `MELLON_PYI_SLIM` | installer build | `0` keeps the OpenGL DLL and translations | — |
| Repository `.gitattributes`, `.gitignore` | user's repository | `* text=auto eol=lf`; `.$*` | CRLF churn; draw.io backups listed by git clients. |

Old data: an `<issue>` element in a manifest is ignored; acks recorded on a merge commit by 0.1 read SUSPECT once (`INSTALL.md`). The link index has a format version; another version is ignored.

## Bought and reused parts (A14)

| Part | Version | Relied on for | How it is checked | Known issues and handling | If it fails or changes |
|---|---|---|---|---|---|
| pygit2 / libgit2 | 1.19.2 (bound `<2`) | History walks, tree diffs, status, index, commits, tags, merge state | Rung 2 tests on real repositories and the `req-test` fixture | 256 MB object cache cliff (avoided by `link_index`); ownership check refuses repos on network drives (`INSTALL.md`) | Every suspect state, commit and Compare depends on it. A new version is rechecked by the suite only. |
| lxml | 6.1.0 (`<7`) | Parsing; byte-stable output (`etree.indent`, header, LF) | Unit tests; written bytes compared in tests | First edit of a hand-written file re-indents it | A change in output bytes rewrites files on the next edit, so links to them go SUSPECT. |
| PySide6-Essentials | 6.11.0 (`<7`) | Widgets, QtSvg, QSettings, thread pool, paths | Rung 3 offscreen tests | QtSvg (SVG Tiny) can't draw draw.io's colours and HTML labels; `drawio.qt_svg` rewrites them | GUI only; the MCP exe has no Qt. |
| mcp (FastMCP) | 1.27.0 (`<2`) | stdio JSON-RPC, tool schemas from signatures | MCP tests call the tools | 2.x removed FastMCP; server version set through a private attribute (`mcp._mcp_server.version`) | A change in how sync tools run would change concurrency. |
| Jinja2 | 3.1.6 (`<4`) | LaTeX templates with `((* *))` delimiters | Export tests | Text is escaped by `escape_latex` | PDF content. |
| tectonic | 0.17.0 bundled; any on `PATH` | LaTeX → PDF | Compile tests skip without tectonic; CI doesn't install it. The installer build compiles the fixture documents. | 300 s timeout with a misleading network message; glyph metric problem worked around in the Traceability tables | Export fails or differs. |
| draw.io desktop | any; not checked | Editing figures; rendering them in the PDF | Faked in tests | A multi-page diagram exports page 1; empty diagrams don't render | Placeholders and a notice. |
| PyInstaller | 6.22.3 | Frozen onedir build | Installer workflow installs and runs `--version` | Copies DLLs from `PATH`; the build trims `PATH` | Installer only. |
| Inno Setup | 6.3+ | Installer | Installer workflow | Older 6.x reject `x64compatible` | Installer only. |
| CPython | 3.12 | Runtime | CI | — | — |

## Diagnostics (A15)

- **Version.** `--version` on all three commands; the About dialog; the MCP `serverInfo`. A bare source tree reports `0+unknown`. The version is **not** written to the GUI error log or the MCP startup log line.
- **GUI error log.** Uncaught exceptions only, with a timestamp and traceback, appended without a size limit. Expected failures (refused commit, failed tag, failed ack) appear in the status bar for 3-8 s or a dialog and are not logged.
- **MCP log.** stderr at INFO by default: the tool name and main arguments for most tools; the client keeps it (Claude Desktop: `%APPDATA%\Claude\logs`).
- **Audit trail.** Commits made through MCP carry a `Via: mellon-mcp` trailer. Git history is the lasting record of every change.
- **Load errors** carry file, line, message and a conflict-marker hint, in every surface.
- **`mellon-check`** prints one line per problem with a code, and a summary.

## Testability (A4)

- Library functions take paths or models and return values; the editor writes plain files. Tests run them on temporary repositories and the `req-test` fixture.
- The service runs without Qt. The GUI runs offscreen with pytest-qt. MCP tools are plain Python functions that tests call directly after setting `_State`. `check.main(argv)` returns the exit code.
- draw.io is faked. tectonic is optional, so PDF compilation is skipped where it is missing, including CI.
- **Harder to test:** module-level state (`mcp.server._State`, the loader's parse caches, `link_index`'s memory and `_use_disk`, the editor's journal stack), which tests must reset; behaviour tied to time (the loader's 2 s window, commit times in `merge`); staleness, which needs a window activation event; and anything across processes or a crash mid-write.

## Size and coupling (A5)

| Warning sign | Where |
|---|---|
| Large modules | `mcp/server.py` 2,581 lines (46 tools); `gui/main_window.py` 2,369; `service/document.py` 2,261 (about 90 methods); `editor.py` 1,853; `gui/panels/grid_pane.py` 1,506; `merge.py` 1,055. 27,400 lines in `src/` in all. |
| Hub | `DocumentService` is the one path for GUI, MCP and `mellon-check`. Most requirements will land on it. It is also a common cause: a bug in it is the same in all three, and `mellon-check` can't catch what the GUI gets wrong the same way. |
| Private names used across modules | `editor._child_path` (service, MCP), `editor._sanitize_text` (service, merge), `editor._write_tree` (suspects), `git_ops._replace_file` (editor, link_index, merge), `git_ops._short` (link_index). |
| Mutations outside the editor | See [Components](#components-a1). |
| Read-only spread over widgets | In the GUI, read-only is checked by each action, model and panel; the service under it is writable. MCP has two layers. |
| Global state | See [Testability](#testability-a4). |
| People | One author knows the code. |

## Open points

Values and decisions this page couldn't determine. Each needs an answer from a person or a measurement, and belongs in [open-questions.md](open-questions.md) when an area takes it up.

1. Power loss during a write: can a file end up empty or partly written without a flush? Should writes be flushed?
2. Memory use at 1,000 and 5,000 objects, and a budget.
3. Performance with many documents in one repository.
4. Size of `.git/mellon-link-index.json` and of the GUI error log, and whether either needs a limit.
5. Which draw.io versions work.
6. Whether macOS is supported.
7. Whether editing should be refused while a merge is in progress (open question Q4).
8. Whether the GUI should check for external changes before each write and commit, not only on window activation (Q5).
9. Whether an acknowledgement should record the commit the reviewer looked at rather than the latest one (Q6).

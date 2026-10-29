# Status

A snapshot of what's built, what's deferred, and where to pick up. For the
per-release list of changes, see [CHANGELOG.md](CHANGELOG.md).

**Release in progress:** v0.2.0, currently `0.2.0rc1` on `develop`, with
draw.io figure support. It ships as a Windows installer (see [Packaging](#packaging-windows-installer)),
and the team routine is in [TEAM.md](TEAM.md). The scale target is about 500
objects per document or fewer.

## What's built

Everything below is implemented and tested. The on-disk format and workflows
are documented in [USAGE.md](USAGE.md).

### Core

- **XML schema:**
  - the manifest `<doc-id>/<doc-id>.xml`
  - objects in `objects/<id>.xml`
  - outgoing links in `links/<id>.xml`
  - figures in `figures/<id>.svg`
  - views in `views/<slug>.xml` and filters in `filters/<slug>.xml`
  - rules in `<doc-id>.rules.xml`
- **In-memory model:** frozen dataclasses in `src/mellon/schema.py`.
- **Loader and validator** with structural checks and the rule engine.
- **Every mutation lives in `editor.py`:**
  - Writes are atomic (temp sibling, then `os.replace`, retried briefly when
    Windows reports the file as busy), and output is always LF with canonical
    2-space indentation.
  - Operations are all or nothing across files too. Every write and delete
    first records the file's previous bytes in the open journals
    (`editor.journal()`); each public mutation runs inside one that puts
    every touched file back if it raises. `suspects.ack_all` likewise.
- **Text sanitising:**
  - `\x0b` / `\x0c` become newlines, and CR / CRLF become LF.
  - Any other C0 control character except tab raises `EditorError`.
  - Slugs for views and filters must match `^[a-z0-9]+(-[a-z0-9]+)*$`.
  - Names that would resolve outside their folder are refused
    (`_child_path`).
- **Repo discovery:** every top-level folder `X` that has `X/X.xml` is a
  document; folders starting with `.` are skipped. A document that fails to
  load is recorded as a `DocLoadError` (doc id, file, line, message, with an
  "(unresolved merge conflict?)" hint when the file has conflict markers)
  instead of disappearing.
- **Git integration via `pygit2`:**
  - **Last-commit hash per path.** The rule is the same as
    `git log -1 -- <paths>`: a merge counts as a change only when the path
    differs from every parent. This hash drives ack tracking.
  - **`check_repo_root`** refuses a folder that:
    - is not in a git repo
    - is not the git work-tree root
    - is a shallow clone
    - is a repo git cannot open
  - **`commit`** builds the tree from HEAD plus only the requested paths. It
    refuses while a merge or other git operation is in progress, and when
    `user.name` / `user.email` are unset.
  - **`discard_paths`** restores a folder to HEAD.
- **Suspect links:** both the object file and its figure count when deciding
  whether a target changed. The service caches suspect scans keyed by HEAD, so
  a commit invalidates them.

### MCP server (45 tools)

`mellon-mcp [--repo-root <path>] [--read-only] [--log-level …] [--version]`
starts an MCP stdio server.

- `--repo-root` defaults to `$CLAUDE_PROJECT_DIR`, then the current directory.
- The server exits with status 2 when the root:
  - is not a directory
  - is not the root of a full git clone (as checked by `check_repo_root`)
  - can't be scanned for documents

Client configuration (`claude mcp add`, Claude Desktop) is in
[INSTALL.md](INSTALL.md).

| Group | Tools |
|---|---|
| **Read (13)** | `list_documents`, `show_document`, `show_object`, `inspect_object`, `outline`, `validate_document`, `validate_repo`, `repo_health`, `suspects`, `trace`, `incoming_links`, `coverage_report`, `search_objects` |
| **Write (27)** | `new_object`, `update_object`, `copy_object`, `bulk_set_attribute`, `set_attribute`, `edit_description`, `edit_metadata`, `tag_issue`, `set_figure`, `clear_figure`, `set_table`, `clear_table`, `add_parent_doc`, `remove_parent_doc`, `add_attr_option`, `define_attribute`, `change_attribute_required`, `delete_attribute`, `remove_attr_option`, `create_document`, `move_object`, `reorder_outline`, `add_link`, `remove_link`, `delete_object`, `ack_suspects`, `manage_view` |
| **Git / review / export (5)** | `commit_changes`, `git_history`, `show_object_at`, `review_suspect_link`, `export_document` |

**Write results and commits**

- Writes return `{ok, affected_files, message, error?, error_type?}`, and
  `affected_files` lists every file the tool touched.
- `--read-only` refuses every write tool, plus `commit_changes` and
  `export_document`. The MCP boundary enforces it, and `DocumentService`
  also refuses writes, tags and commits.
- Every write tool except `tag_issue` accepts `commit_message=…`;
  `export_document` doesn't either (a PDF isn't repo content).
  - With it, the tool commits the touched files with a `Via: mellon-mcp`
    trailer and adds `{commit: {short_hash, commit_message}}`, or
    `commit_error` on failure. The write is never rolled back.
  - The default is `None`, so commits stay explicit.
- `commit_changes(message, paths=None)` commits only uncommitted changes
  inside document folders when `paths` is omitted. It appends the trailer by
  default.

**Filesystem paths.** Only two tools accept a filesystem path, and both
resolve relative paths against the repo root:

- `set_figure` reads the SVG at `source_path`.
- `export_document` writes the PDF to `out_path`, which must end in `.pdf`
  and must not be inside `.git`. Its `rev` defaults to the doc's current
  issue tag, and `""` suppresses it. Figures are rendered with draw.io
  desktop; the result lists `unrendered_figures` (plus `drawio_found: false`
  when draw.io isn't installed). The PDF isn't committed.

**Load errors.** A document that fails to load is still reported:

- `list_documents` lists it as `{doc_id, folder, load_error}`.
- `validate_repo` and `repo_health` carry `load_errors`.
- `validate_repo` is `ok: false` while any document fails to load.

**Shapes worth knowing** (from the agent-friendliness pass):

- **`list_documents` / `show_document`** default to a compact shape (`doc_id`,
  `folder`, `object_count`, `link_count`, `current_issue`). Pass
  `include=["metadata", "parents", "attributes", "outline", "views", "filters"]`
  (or `["all"]`) for the heavier fields.
- **`validate_document`** returns `[{code, message}]` with stable validator
  codes (`ATTR_MISSING_REQUIRED`, `OUTLINE_DUP`, `LINK_TARGET_MISSING`, …).
  `validate_repo()` aggregates this across every document.
- **`error_type: "AlreadyExists"`** marks duplicate-create failures
  (`add_link`, `define_attribute`, `add_attr_option`, `add_parent_doc`,
  `create_document` on an existing folder).
- **`trace`** defaults to `max_depth=2`. **`coverage_report`** returns totals
  only, unless you pass `include_lists=True`.
- **`ack_suspects`**:
  - `ack_suspects(link_keys=[{source_id, target_doc, target_id}, ...])` acks
    a subset of the suspect links; `None` acks every SUSPECT link in the doc.
  - `dry_run=True` reports `would_acknowledge` without writing.
- **`repo_health()`** returns, in one call:
  - per-doc counts (objects, links, validation errors, suspects, broken)
  - pairwise coverage rows for every declared parent relationship
  - `load_errors`
- **`search_objects(query, *, doc_ids?, attr_filters?, max_results=50)`** is a
  case-insensitive substring scan over object ids, attribute values and
  descriptions, with ranked hits and snippets.
- **`inspect_object`** is `show_object` + `trace(both, depth=2)` +
  `git_history` in one call.
- **`define_attribute(default_value=…)`** backfills the value on every
  existing object.
- **`manage_view(doc_id, action="create"|"rename"|"delete"|"edit", …)`** is
  the one view tool. There are no MCP tools for filters or rules.
- **`tag_issue(doc_id, issue)`** tags HEAD. The issue must be dot-separated
  numeric and higher than the current issue, and an existing tag is refused.

**Breaking renames (PR 5):** `edit_attribute` is now `set_attribute` and
`add_attribute` is now `define_attribute`.

### GUI

`mellon-gui [path] [--version]` opens a `ProjectExplorerWindow`.

- It refuses a folder that isn't the root of a full git clone, and explains
  which folder to open instead.
- Uncaught exceptions are logged to
  `%LOCALAPPDATA%\mellon\mellon\logs\mellon-gui.log` (Qt's
  `AppLocalDataLocation`; it falls back to the temp folder) and shown in a
  rate-limited dialog.

**Project Explorer**

- The sidebar lists `DOCUMENTS` and `OPEN`.
- A document that fails to load is listed greyed, with a warning icon. The
  tooltip shows `file:line` and the message, and a double-click shows the
  error instead of opening an editor.
- **Toolbar:** a hamburger **Menu** and **Document** (`Ctrl+N`, new
  document).
- **Menu:**
  - Open Repository… (`Ctrl+O`) and Quit
  - **View:** Refresh (`F5`), which drops the cache and re-lists every
    document; Theme (Light / Dark / Follow system)
  - **Help:** About
- **Status bar:** `branch @ shorthash · N dirty`.
- When the window is activated, it re-lists documents if a document folder
  appeared or disappeared. A document that breaks in place shows as failed
  only after F5.
- Opening another repository closes the previous repository's editor
  windows.
- **Merge banner:** while a git merge is in progress (checked on load, F5
  and activation), an amber banner shows the number of conflicted files and
  **Resolve merge…**, or, with none left, says to make the merge commit in
  the git client. See [Merging branches](#merging-branches).

### Merging branches

`merge.py` resolves a git merge that stopped with conflicts in documents.
Git merges document files as text: two branches that each created the next
object both call it `<doc>-<n>`, the manifest gains a duplicate outline ref
without complaint, and links one branch made to its new object land on the
other's.

- **`plan(root)`** reads the merge base, HEAD and MERGE_HEAD (not the
  conflict markers) and returns `MergePlan(into, merging, renumbered,
  by_hand, outside)`.
- **Renumbered** (an id new on both sides, different objects): not a
  conflict. The object committed later yields; its side is rewritten as if
  it had used the next id free on both sides — object, links and figure
  files, outline ref, and every link that side made to it. All links to an
  id new on one side were made on that side, so the retargeting is exact.
- **Three-way merge** of every document file both sides changed, on the
  renumbered states: one-sided changes take that side; objects merge field
  by field (each `attr`, the description, the table); links files merge as
  sets keyed by target (a link changed on both sides keeps ours); the
  manifest's outline merges by object position (theirs' additions, moves and
  deletions applied to ours; a double move keeps ours) and its properties
  are git's clean merge or merged per field (metadata, parents as a set,
  attribute definitions by name).
- **By hand** (`HandMerge`): a field both sides changed (`FieldConflict`,
  kind `text` for the description and metadata, `value` for an attribute,
  `whole` for a table, figure, attribute definition, view, filter or rules),
  or an object deleted on one side and changed on the other. A requirement's
  hand merge carries `links`: what it traces to, with the targets' merged
  text.
- **`resolve(root, edits=, delete=)`** takes merged texts or `"ours"` /
  `"theirs"` per field (default ours) and the deleted-vs-changed objects to
  delete (default keep), reconciles each outline with the objects kept,
  writes the files and marks them resolved in the index. The merge commit
  stays with the git client. Paths outside document folders are reported,
  not touched. Re-runnable: it recomputes from the commits.
- GUI: the Project Explorer's banner and **Resolve merge…** dialog
  (renumbered lines; one block per hand merge with **Traces to**, an
  editable merged text, value dropdowns; every choice defaulted). No MCP
  tools.

**Document Editor** (one window per document). It has three collapsible
panes, and pane sizes are saved per document:

- **Left:** `OutlinePane`, an outline tree with the outline edit actions.
- **Center:** `GridPane`, the objects grid. It has a state gutter, id,
  description and attribute columns, a free-text filter box, the
  advanced-filter pill, and the suspect, read-only and stale banners. When it
  is empty it shows "This document has no objects yet." [Add first object],
  or "No requirements match the current filter." [Clear filters].
- **Right:** `RightPane` tabs:
  - **Inspector:** attributes, description, and outgoing and incoming links.
    A suspect outgoing link has an Ack button in a writable window.
    Right-click an outgoing link for Jump, and for Remove when the window is
    writable.
  - **History:** the git log of the selected object's file, with "View at
    this commit…".

**Editing**

- Read-only is the default. Turn it off with Menu → Read-only or the banner's
  **Enable editing**. A successful commit returns the window to read-only.
- **Grid cells use kind-aware editors:**
  - `QComboBox` for enum and boolean, with an empty entry so an empty cell
    stays empty
  - spin boxes for integer and real
  - `QDateEdit` for date
  - a multi-line editor for the description
  - a line edit for text

  An unchanged value writes nothing.
- Inspector fields commit on focus-out, or on change for combos.
- Before any action (New, Delete, Link, Commit, …), text that was typed but
  not committed is saved first (`flush_pending_edits`).
- Description rows grow to fit (no ellipsis).

**Shortcuts and actions**

| Action | Key |
|---|---|
| New sibling / New child | `Ctrl+N` / `Ctrl+Shift+N` |
| Indent / Outdent | `Ctrl+]` / `Ctrl+[` (in the grid also `Tab` / `Shift+Tab`) |
| Move up / Move down | `Alt+↑` / `Alt+↓` |
| Delete object | `Del` |
| Commit | `Ctrl+S` or `Ctrl+K` |
| Export PDF | `Ctrl+E` |
| Add link / Trace | `Ctrl+L` / `Ctrl+T` |
| Attach or replace figure | `Ctrl+G` |
| Filter box / Advanced filter | `Ctrl+F` / `Ctrl+Shift+F` |
| Reload from disk | `F5` |
| Undo / Redo | `Ctrl+Z` / `Ctrl+Y` or `Ctrl+Shift+Z` |
| Close window | `Ctrl+W` |

New, Child, Indent/Outdent (`Ctrl+]` / `Ctrl+[`), Move up/down and `Del` sit
on the outline pane's toolbar; their shortcuts also work while that pane is
hidden. `Tab` / `Shift+Tab` in the grid indent and outdent too.

The toolbar also has Validate, Properties (metadata, parents, attributes), and
the **Figure**, **Table**, **Views** and **Filters** dropdowns. The hamburger
**Menu** holds:

- Export
- **Discard uncommitted changes to this document…**
- Close
- Undo and Redo, Find and Advanced filter
- Reload from disk, Read-only and Theme
- About

**Undo / Redo** (`Ctrl+Z` / `Ctrl+Y`) step back through this window's edits
since the last commit; a focused text field keeps `Ctrl+Z` for its own text.

- The service records the files each write call changes (before and after
  bytes) and tells its change listeners; each window keeps its own
  document's steps. One call is one step: a composite update, or a delete
  that strips links in other documents.
- Undo puts every file of the step back and reloads; pending typed text is
  written first, so `Ctrl+Z` after typing undoes the typing.
- The history is cleared on a commit, `F5` / Discard, the "Changed on disk"
  banner, and a HEAD move that leaves the document with nothing
  uncommitted. Draw.io saves aren't steps (draw.io has its own undo).
- A step whose files changed since (MCP, another window, draw.io) is
  refused with a message rather than clobbering them; the history goes.
- Disabled while read-only. MCP has no undo.

**Delete** always asks for confirmation: `Ctrl+Z` brings the object back,
but a delete can also strip links in other documents.

- An object that has sub-objects in the outline can't be deleted until they
  are moved or deleted.
- When other objects link to it, a second confirmation offers to strip those
  incoming links too.
- The figure is deleted along with the object.
- Deletion is refused while any document in the repo fails to load.

**Discard uncommitted changes to this document…** puts the whole document
back to its last commit in one go.

- It puts the document folder back to HEAD: edits are reverted, and objects
  added since the last commit are deleted.
- It asks first. It is enabled only when the document is writable, has
  uncommitted changes and is in the last commit.
- Other documents are not touched.

**Commit dialog** (`Ctrl+S` / `Ctrl+K`). It shows the commit message, the
files to commit, a warning for dirty parent documents, and the suspect links
on this document.

- Only the document's own folder is committed.
- **Issue:** prefilled with the current issue (empty when there is none).
  - Leaving it unchanged makes a plain commit with no tag.
  - Raising it cuts `<doc-id>-issue-<n>` at the new commit. It must be higher
    than the current issue.
- **Author and date:** prefilled from the manifest. The date is never set to
  today automatically.
- An author/date edit is written to the manifest before the commit and put
  back if the commit fails.
- Git errors (no identity, merge in progress) are shown in full.

**Links, trace and suspects**

- `Ctrl+L` opens the `LinkPicker`, and `Ctrl+T` opens `TraceView`.
- The grid's `state` gutter marks suspect and broken links.
- When the document has suspect links, an amber banner offers **Suspects
  only** (a filter toggle) and **Ack selected** (acks every suspect link on
  the selected object). Single links can be acked from the Inspector.
- On window activation, link states are re-read if HEAD has moved. That
  covers a commit made in this window, in another window, or in a git client.

**External changes**

- Each editor fingerprints its document folder and its declared parent
  folders (`repo.folders_fingerprint`, mtime and size). On window activation,
  a mismatch shows the amber **Changed on disk** banner.
- While the banner shows, edits are refused with "Changed on disk. Reload
  first (F5)."
- **F5** (Menu → Reload from disk) drops the shared cache and reloads. If the
  document now fails to load, F5 shows the real error and keeps the last good
  copy behind the banner.
- Own-window edits never trigger the banner.
- Neither does a figure of this document rewritten in place (draw.io saving
  after "Edit in draw.io"), nor draw.io's `.$*` backups beside it: figure
  contents are never cached, so `RepoController.changed_figures` lets those
  through and the grid just repaints its thumbnails and dirty markers. A
  figure added or removed, any other file, or a parent's folder still counts.
- By design there is no file watcher, auto-reload or merge UI.

**PDF export** (`Ctrl+E`) runs in a background thread. It shows a notice when
attached figures were left out of the PDF, saying whether draw.io was missing.

**Status bar:** `branch @ shorthash · N dirty` (with a tooltip for a detached
HEAD).

**Themes:** light and dark tokens in `gui/theme.py`.
`ThemeManager` applies a `QPalette` plus QSS and saves the choice in
`QSettings`.

**Saved views** (column visibility and order) live at
`<doc>/views/<slug>.xml`.

- The **Views** dropdown lists them and offers New view… and Manage views….
- Right-click the grid header for a quick column toggle.
- The State, ID and Description columns are always shown.
- Over MCP, views are managed with `manage_view`.

**Saved filters** (DOORS-style, multi-rule) live at `<doc>/filters/<slug>.xml`.

- `Ctrl+Shift+F` opens the Advanced filter dialog, with one row per rule:
  attribute, then an operator for that attribute's kind, then a value.
- **Operators per kind:**

  | Kind | Operators |
  |---|---|
  | enum | `eq`, `ne`, `is-empty`, `is-not-empty` |
  | text | `contains`, `not-contains`, `starts-with`, `eq`, `ne`, `is-empty`, `is-not-empty` |
  | integer, real | `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `between`, `is-empty`, `is-not-empty` |
  | boolean | `is-true`, `is-false`, `is-empty` |
  | date | `eq`, `ne`, `before`, `after`, `between`, `is-empty`, `is-not-empty` |

- **Link-presence rules:** `has-outgoing-link [to <doc>]` and
  `has-incoming-link [from <doc>]`.
- **Matching:** `Match: All` / `Any`. "Keep ancestor headings visible" is on
  by default.
- **In the window:**
  - A pill above the grid shows the active filter, with `[Edit]` / `[×]`.
  - The **Filters** dropdown lists the saved filters.
  - The active filter is remembered per document.
  - The saved filter is ANDed with the text filter and the suspects-only
    toggle.

### Figures

- **Storage:** one SVG per object at `<doc-id>/figures/<obj-id>.svg`, found by
  filename. A draw.io SVG carries the editable diagram in its root `content`
  attribute, so the one file is both picture and draw.io source.
- **draw.io integration** (`mellon/drawio.py`, stdlib + lxml so the frozen
  MCP server can use it): `find_drawio` (`MELLON_DRAWIO` → the Windows install
  folders → `PATH`), `is_drawio_svg`, `BLANK_DRAWIO_SVG`, `open_in_drawio`,
  `qt_svg`, `export_pdfs`.
- **Where it shows:** the editor, service and MCP (`set_figure`,
  `clear_figure`, and the `figure` field on `show_object`) all support it. The
  grid's description column paints a thumbnail.
- **Entry points:**
  - the **Figure** toolbar dropdown (New draw.io figure / Edit in draw.io,
    Attach / Replace, Clear, View full size), with `Ctrl+G` for attach or
    replace
  - the right-click row menu, with the same draw.io action
  - double-clicking the thumbnail, which opens `FigureViewDialog`

  It deliberately isn't in the Inspector.
- **Edit round trip:** "New draw.io figure…" attaches `BLANK_DRAWIO_SVG`
  through the ordinary `set_figure` and opens it in draw.io desktop; "Edit in
  draw.io…" opens the existing file (disabled for plain SVGs). draw.io saves in
  place, and window activation picks the save up without the stale banner
  (see External-change detection).
- **Rendering in Qt:** QtSvg (SVG Tiny) paints draw.io's `light-dark()` /
  `var()` colours black and can't draw its HTML labels. `drawio.qt_svg`
  resolves the colours, drops the "Text is not SVG" notice and turns old-style
  truncated `<text>` label fallbacks into wrapping `<textArea>`s; current
  draw.io stores each label's picture as the fallback, which Qt draws as is.
  Thumbnails and the viewer paint figures on white.
- **Change tracking:** figure files count for dirty detection, for the commit
  dialog's affected requirements, for suspect detection and for object
  history. A figure-only edit makes children suspect, and deleting an object
  deletes its figure. draw.io's `.$*` backup files are never committed.
- **PDF export** renders figures through draw.io (see PDF export).

### Tables

A table is structured content on one object: `<table>` inside
`objects/<id>.xml`.

- **Structure:**
  - `<columns>` with per-column alignment
  - `<row header>` for header rows
  - `<cell colspan rowspan>` for merged cells

  By convention the object's `type` is `Table` and its description is the
  caption. Rendering, export and editing key off `obj.table is not None`, not
  the convention.
- **Geometry** is checked by the shared `schema.table_geometry_errors`:
  - the loader treats a bad geometry as a hard error
  - `editor.set_table` raises `EditorError`
  - the validator reports `TABLE_GEOMETRY`, `TABLE_BAD_ALIGN`,
    `TABLE_BAD_SPAN` and `TABLE_NO_COLUMNS`
- **Export** produces a `longtable` (booktabs, `\multicolumn`, `\multirow`).
  Leading header rows end the longtable head (`\endhead`), so they repeat on
  every page, unless a header rowspan reaches into the body.
- **GUI:** one rendering engine. The read-only preview and the editor are the
  same `InlineTableWidget` (`gui/widgets/inline_table.py`). The preview is a
  cached `grab()` of it, and double-click opens it live in the same place.
  - The caption is painted by the delegate above the grid.
  - `QTextDocument` is used only to measure (caption height, column widths).
- **Controls** are all in the toolbar's **Table** dropdown:
  - insert / remove the table
  - add / delete a row or column
  - merge / split cells
  - header row
  - column alignment

  The editor commits on focus-out or `Ctrl+Enter`, and Esc cancels.
- Cells are deliberately not objects (no DOORS "cells-as-objects").

### PDF export

- **Library:** `mellon.export.pdf(doc, *, folder=None, attrs=..., rev=None,
  unrendered=None) -> bytes`.
  Jinja2 renders LaTeX with `((* *))` / `((( )))` delimiters, using the
  bundled `templates/syrs/main.tex.j2` + `syrs.cls`, and tectonic compiles it.
- **Outline-aware:**
  - heading objects become the class's `\syrssection` … `\syrsparagraph` by
    depth (section to paragraph level; `\subparagraph` below depth 4)
  - other objects render as a `requirement` block with an `\attrs{…}` row
    (default attributes `type`, `status`, `priority`, `verification-method`)
  - tables become `longtable`
  - figures (given the doc `folder`) are rendered by draw.io desktop in one
    batch run (`drawio.export_pdfs`; plain SVGs are wrapped as a picture in a
    one-cell diagram) to cropped vector PDFs, placed next to the .tex and
    printed by `\syrsfigure` — scaled down to fit, numbered, captioned with the
    description on ordinary objects (replacing the `\shall` row) and with
    "Figure N" alone on headings and tables. A figure that didn't render, or
    every figure when draw.io is missing, prints a placeholder line and is
    reported through the `unrendered` out-list. draw.io isn't started for a
    document without figures.
- **Traceability section:** with `parents` (doc id → `Document`, from
  `DocumentService.parent_documents`), a document that declares parents ends
  with a numbered "Traceability" section on a new page: a summary of counts;
  "Links to …", a link table (object id | parent ids) with one row per
  non-heading object; then "Uncovered objects in …" per parent: the ids no
  object links to (`coverage_report`'s direct, headings-excluded rule), or a
  note when that parent didn't load. The section turns off XeTeX glyph
  metrics (`\tracetablesetup`): in tectonic's later passes some 9 pt words in
  table cells came out 14 pt tall and spread their lines apart.
- **Suspect-link check before export:** the PDF shows no link states. The
  GUI's Export scans the document's outgoing links first and, if any are
  suspect or broken, warns with **Export anyway** / **Cancel** (default)
  before the save dialog. MCP `export_document` exports anyway and returns
  them in `suspect_links`. Without git there is nothing to check.
- **Cover, running header and PDF title/author** come from `doc.id`,
  `metadata.title` / `author` / `date` and `rev`. Empty fields are left off,
  and there are no hard-coded placeholders.
- **Surfaces:** MCP `export_document` and the GUI's Export (`Ctrl+E`, in a
  background thread).
- **tectonic lookup:** `MELLON_TECTONIC`, then `<exe-dir>/tools` (frozen
  build), then `PATH`, then `<project-root>/tools` (source checkout).
  - `<tectonic-dir>/tectonic-cache` is used as `TECTONIC_CACHE_DIR` when that
    variable is unset.
  - tectonic runs with no console window, a 300 s timeout and UTF-8 decoding.

### Issue tags

- A document's release issue is a lightweight git tag `<doc-id>-issue-<n>`
  (for example `VCU3-SRS-issue-2` or `VCU3-SRS-issue-12.23.55`), not a
  manifest field.
- `current_issue` is the highest `<doc-id>-issue-*` tag reachable from HEAD.
- **Creating tags:**
  - In the GUI, raising the issue field in the commit dialog cuts the tag at
    the new commit. A tag failure is a warning and never rolls back the
    commit.
  - Over MCP, `tag_issue` tags HEAD.
  - Either way, the new issue must be higher than the current one.
- Tags are lightweight; push them with `git push --tags`.
- An `<issue>` element left in a manifest written before issue tags is
  ignored.

### Tags

- The commit dialog's optional **Tag** section (Name + Message) tags the new
  commit with a free name, such as `CDR-1.0`: a snapshot of every document as
  last committed.
- The tag is annotated (tagger and date).
- Names are checked as typed: git's rules, taken names, and the reserved
  `<doc-id>-issue-<n>` form.
- While a name is entered, the dialog warns about the open points of the
  documents this one links up to, transitively: uncommitted changes, suspect
  or broken links, validation errors, failed loads. It never blocks.
- A tag failure after the commit is a status-bar warning; the commit stands.
- With nothing to commit, Commit stays enabled and the dialog's button turns
  into **Tag**: it tags HEAD without a new commit (the milestone case).
- Library: `git_ops.create_tag` / `tag_name_error` / `tag_names`,
  `DocumentService.commit_document(tag=, tag_message=)` / `tag_head`, and
  `DocCommitPreview.upstream_warnings`.
- GUI only: no MCP tool and no tag list or browser.

### Schema kinds and rules

- **Kinds:** `enum`, `text`, `integer`, `real`, `boolean`, `date`.
  - Non-string values are stored as canonical strings and type-checked
    (`ATTR_BAD_INTEGER`, `ATTR_BAD_REAL`, `ATTR_BAD_BOOLEAN`,
    `ATTR_BAD_DATE`).
  - A required attribute that is missing or empty is reported once, as
    `ATTR_MISSING_REQUIRED`.
  - An empty value clears an enum.
- **Rule engine** (`<doc-id>.rules.xml`):
  - `<when>` predicates AND-match. The reserved names `type`,
    `requirement-type` and `has-outgoing-link` keep their meaning; any other
    name matches `obj.attrs[name] == value`.
  - `<then>` takes `<attr name=… status=required|optional|forbidden>` and
    `<link target-doc=… status=required>` (`RULE_LINK_REQUIRED_MISSING`).

### Library-only modules

- `diff.py`: `compare_document(repo_root, doc_id, ref_a, ref_b)` returns a
  `DocumentDiff`. It covers object membership, plus attribute, description and
  outgoing-link deltas for modified objects. It has no MCP or GUI surface (see
  Deferred).

### Packaging (Windows installer)

`packaging/build.ps1` builds `build\Mellon-Setup-<version>.exe`. It needs uv,
git, Inno Setup 6.3+ and internet access. The script:

1. Creates a locked runtime venv (Python 3.12) and installs PyInstaller
   6.22.3.
2. Runs PyInstaller with `mellon.spec`, an onedir build. `mellon.exe` is
   windowed and `mellon-mcp.exe` is a console exe.
3. Downloads tectonic 0.17.0, with a SHA-256 check, into `tools\`.
4. Pre-warms `tools\tectonic-cache` by exporting the `req-test` documents,
   with their Traceability sections.
5. Copies `LICENSE`, `THIRD-PARTY-NOTICES.txt` and `TEAM.md`.
6. Runs `iscc` on `mellon.iss`.

The **Installer** workflow (`.github/workflows/installer.yml`) runs the script
on a Windows runner, on demand or for a `v<version>` tag. It installs the
result as a smoke test and keeps it as a run artifact. For a tag, it also
attaches it to a draft GitHub release.

The installer (`mellon.iss`):

- installs per user into `%LOCALAPPDATA%\Programs\Mellon` with no admin rights
  (`PrivilegesRequired=lowest`)
- has a fixed `AppId` and hides the directory page
- removes the previous `_internal` folder on upgrade
- adds a Start-menu shortcut and does not change PATH

The exes are unsigned: SmartScreen may show "More info → Run anyway" for a
download from the web. Distributing from an intranet share usually avoids
that, but Windows may still show an "Unknown Publisher" warning if the share
isn't in the Local intranet zone.
Close Mellon and Claude Code before upgrading.

**Developers** can still use `uv sync` / `uv run` / `uv tool install` on any
OS (see [INSTALL.md](INSTALL.md)). The PyPI name `mellon` is taken, so the
package is not published there.

### Tests

**1206 tests** (`uv run --no-sync pytest --collect-only -q`, 2026-10-21).
They cover the library, git_ops, MCP read/write/git, and the GUI (smoke,
edits, outline, links, suspect, polish, theme, filter, reload, commit,
export, tables, figures, app). They also cover the filter engine, coverage,
search, diff, schema kinds, the rules engine, issue tags, LaTeX export and
the draw.io integration (`test_drawio.py`, which fakes the draw.io process).

- PDF compilation tests are skipped when `tectonic` is missing.
- GUI tests run headless with `QT_QPA_PLATFORM=offscreen`.
- `MELLON_REQUIRE_FIXTURE=1` makes a missing fixture an error instead of a
  skip. CI sets it.

## Deferred / pending

Ordered roughly by usefulness.

### Compare dialog

There is no GUI Compare dialog yet. `diff.py` stays a library-only module,
with no MCP diff tools.

### Known limitations in 0.2.0

- Figures need draw.io desktop to be edited from Mellon or rendered in the
  PDF; without it they print a placeholder. A multi-page diagram exports its
  first page. Mellon's thumbnail of an old draw.io SVG (text label fallbacks)
  approximates its labels.

By design, not a limitation: the commit dialog commits one document at a
time. That keeps each document's history and issues self-contained. Changes
in other documents, such as incoming links stripped by a delete, get their
own commit.

### Figures follow-ups

PNG and native `.drawio` support, a list of figures in the PDF, alt text,
drag-and-drop attach, a visual diff of figure revisions.

### Tables follow-ups

Inline cell formatting (bold, math), per-cell links if they are ever needed.

### Rich text / markdown in descriptions

Descriptions are plain text for now. This is an open question in
`../req-test/PLAN.md` (the original format plan, kept in the fixture repo).

### GUI follow-up ideas

- A real quick-switcher overlay (`Ctrl+P`).

### Schema extensions parked in `../req-test/PLAN.md`

- C-like macros (shared text fragments).
- Cross-repo links.
- DOORS "table-of-objects" (cells as objects): intentionally not built; see
  [Tables](#tables).

### Out of scope for v0.2.0

- **Features:**
  - in-app pull/push/merge UI
  - init-repo wizard
  - Compare dialog / MCP diff tools
  - rich text
  - `Ctrl+P` switcher
  - cross-repo links
  - roles and approvals
- **Structural performance work:**
  - a single-walk suspect map
  - incremental grid updates
  - batch editor primitives
  - a reverse-link index

  Revisit these if the team hits lag at around 500 objects.
- **Other changes:**
  - a port to mcp 2.x (the `<2` cap is enough)
  - MCP pagination
  - harmonising MCP parameter names (that would break agents)
  - `manage_filter` and rules MCP tools
  - monorepo or subfolder layouts (they are refused instead)
- **Distribution:**
  - auto-update
  - MSIX or MSI packages
  - onefile builds
  - publishing to PyPI
- **Low items, left as they are:**
  - loader whitespace stripping
  - trace diamonds
  - outline whitespace residue
  - the legacy `requirement-type` rule

## Where files live

```
src/mellon/
  schema.py      dataclasses (Document, Metadata, Attribute, Table, View, Filter, …)
  loader.py      XML → Document
  validator.py   structural checks + rule engine
  editor.py      all mutations; atomic LF writes, write journal (all-or-nothing
                 operations, undo data), text sanitiser, _child_path guard
  git_ops.py     pygit2 wrappers: discover_repo, check_repo_root, last_commit_for_path(s),
                 commit, discard_paths, show_blob_at, log_for_path, head_summary,
                 dirty_count, dirty_paths_under, current_issue, create_issue_tag
  suspects.py    scan_document, scan_repo, ack_all
  repo.py        discover_documents, load_all (+ DocLoadError), cross-doc incoming,
                 all_objects, folders_fingerprint / doc_folder_names (staleness checks)
  trace.py       recursive up/down traversal, cycle-safe
  filter.py      filter engine: evaluate(doc, filt) → set[obj_id]; OPS_BY_KIND, OPERATORS_LABEL
  coverage.py    CoverageRow, CoverageReport, coverage_report (direct + transitive, headings-aware)
  search.py      SearchHit, search_objects (substring scan, ranked)
  diff.py        compare_document → DocumentDiff (library only)
  merge.py       resolve a conflicted git merge of documents: pending, plan, resolve
                 (id clashes renumbered, link sets, outline merge, mine/theirs picks)
  export.py      LaTeX/PDF export: render_latex, build_pdf, pdf, unrendered_figures (tectonic)
  drawio.py      draw.io desktop: find_drawio, is_drawio_svg, BLANK_DRAWIO_SVG, open_in_drawio,
                 qt_svg (QtSvg-friendly rewrite), export_pdfs (batch CLI render)
  templates/syrs/  main.tex.j2 (incl. the Traceability section) + syrs.cls
  mcp/
    server.py    FastMCP stdio server; 45 tools: read (13) / write (27) / git+review (5)
    serialize.py dataclass → JSON-serializable dict helpers
  service/
    document.py  DocumentService (the only object MCP/GUI talk to for mutations);
                 change listeners + undo_changes / redo_changes
    cache.py     RepoCache: loaded docs, load errors, HEAD-keyed suspect scans
    errors.py    ServiceError + ErrorCode
    results.py   result types (MutationResult, CommitResult, DocCommitPreview, …)
  gui/
    app.py                         QApplication entrypoint (mellon-gui), excepthook + error log
    project_explorer_window.py     ProjectExplorerWindow: doc list (incl. failed docs) + open editors,
                                   MergeBanner
    main_window.py                 DocumentEditorWindow: three-pane editor for one document, undo/redo
    controller.py                  RepoController (root guard, load, selection, Qt signals)
    theme.py                       ThemeManager singleton; tokens, QPalette+QSS, build_theme_menu
    delegates.py                   per-column grid delegates (state gutter, id, multi-line text +
                                   table preview, attr-kind)
    settings.py                    QSettings factory (org/app names)
    dpi.py                         high-DPI helpers
    branding/                      app/window icons, About-dialog logo
    icons/                         theme-aware vector icons
    models/                        ProjectTreeModel, OutlineModel, ObjectsGridModel
    widgets/inline_table.py        InlineTableWidget: table preview + in-place cell editor
    panels/                        OutlinePane, GridPane (+ banners), RightPane, Inspector, HistoryTab
    dialogs/                       LinkPicker, TraceView, CommitDialog, DocumentPropertiesDialog,
                                   FilterDialog + ManageFiltersDialog, NewViewDialog +
                                   ManageViewsDialog, NewDocumentDialog, ObjectAtCommitDialog,
                                   FigureViewDialog, MergeDialog, AboutDialog

packaging/       build.ps1, mellon.spec, mellon.iss, launch_gui.py, launch_mcp.py,
                 THIRD-PARTY-NOTICES.txt
tests/           one file per module; conftest.py holds the fixture + a test git identity
tools/           (gitignored) optional vendored tectonic for source checkouts
CHANGELOG.md     per-release changes
TEAM.md          team routine (also shipped in the installer)
INSTALL.md       installer + MCP client config + dev env setup
USAGE.md         on-disk format, schema rules, suspect workflow, library API
STATUS.md        this file
LICENSE          GPL-3.0-or-later
```

## Picking this up on another machine

1. Install `uv` and clone the repo; see [INSTALL.md](INSTALL.md).
2. Run `uv sync` in `mellon/` to build the env from `uv.lock`. If the env
   predates 0.2, run
   `uv sync --reinstall-package pyside6-essentials --reinstall-package shiboken6`.
3. Set a git identity (`git config --global user.name` / `user.email`). Commits
   refuse without one; the tests provide their own.
4. Run `uv run pytest`. On the first run, `tests/conftest.py` clones the
   fixture repo from `../req-test.bundle` into `../req-test/`. Expect all
   green.
5. Read this file and [CHANGELOG.md](CHANGELOG.md) to see where we left off.

## Fixture repository (`req-test/`)

The fixture holds the git history that the MCP and GUI integration tests
assert against, such as the commits the suspect flow acknowledges. Tests find
those commits by message through `fixture_commit` in `tests/conftest.py`, not
by hash. A nested git repo inside the outer code repo wouldn't work, so it's
packaged as a single file:

```
./                    # outer git repo
├── req-test.bundle   # checked into the outer repo
├── req-test/         # unpacked, .gitignored
└── mellon/           # source tree (the Python project)
```

`tests/conftest.py::_ensure_req_test_fixture()` automatically runs
`git clone req-test.bundle req-test` when the unpacked folder is missing.

### Regenerating the bundle after editing the fixture

After you commit a change inside `req-test/` that everyone else should pick
up:

```
# from repo root
git -C req-test bundle create ../req-test.bundle --all
git add req-test.bundle && git commit -m "Refresh req-test fixture"
```

`--all` packs every ref (branches and tags). Committing the `.bundle` blob
updates the fixture for the next clone.

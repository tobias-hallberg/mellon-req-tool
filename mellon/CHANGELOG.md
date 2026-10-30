# Changelog

All notable changes to Mellon. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## 0.3.0 — 2026-10-30

The first public release. It includes everything listed under 0.2.0 below,
which never shipped on its own.

draw.io support for figures: draw and edit them in draw.io desktop from
Mellon, see them properly in the grid, and get them in the PDF. The PDF also
gets a Traceability section, and the Document Editor gets undo and redo.
Mellon resolves branch merges of documents, so work on several branches no
longer clashes over object ids. A commit can now be tagged with a free name
(e.g. `CDR-1.0`) to mark the whole document set at a milestone.

### Added

- **Tag section in the commit dialog.** Below the document metadata, an
  optional **Name** and **Message** tag the new commit: an annotated git tag
  that marks every document as last committed. Names are checked as you
  type (git's rules, names already taken, and the `<doc-id>-issue-<n>` form,
  which stays reserved for issues). While a name is entered, the dialog
  lists the open points of the documents this one links up to, all the way
  up the link chain: uncommitted changes, suspect or broken links,
  validation errors, documents that fail to load. These are warnings only.
  With nothing to commit, Commit stays available and the button turns into
  **Tag**, which tags the current commit. Push with `git push --tags`.
  Library: `git_ops.create_tag`, `DocumentService.commit_document(tag=,
  tag_message=)` and `DocumentService.tag_head`.
- **Traceability section in the PDF.** A document that declares parent
  documents ends with a numbered Traceability section: a link table with
  one row per object (its id and the parent objects it links to), then per
  parent the ids of the objects nothing links to, and a summary of the
  counts. GUI Export and MCP `export_document` both include it. Library:
  `export.pdf(..., parents=)` and `DocumentService.parent_documents`.
- **Suspect links are checked before export.** If the document has suspect
  or broken outgoing links, Export warns and lists them, with **Export
  anyway** or **Cancel**. MCP `export_document` returns them in
  `suspect_links`.
- **Table header rows repeat on every page** a table spans in the PDF.
- **Undo / Redo** (Ctrl+Z / Ctrl+Y, Menu → Edit) in the Document Editor,
  back to the last commit. Each edit is one step with every file it changed,
  so undoing a delete also restores the links it stripped in other
  documents. The history clears on commit, F5 / Discard and the "Changed on
  disk" banner; a step whose files changed since is refused rather than
  overwriting them. Not for draw.io saves or MCP. Library: the service's
  change listeners and `undo_changes` / `redo_changes`.
- **Resolve merge…** in the Project Explorer. While a git merge is in
  progress, a banner shows the conflicted files. Requirements both branches
  created under the same id are renumbered without asking (the one committed
  later moves to the next free id, with its links, figure, outline place and
  the links made to it). Requirements merge per field, so only a field both
  branches changed needs a hand: an editable merged text, or a value to
  pick, shown with the requirements it traces to. Link files and outlines
  merge by themselves; every choice has a default. Then make the merge
  commit in your git client. Library: `mellon.merge.plan` / `resolve` /
  `pending`.
- **New draw.io figure / Edit in draw.io** in the Figure toolbar menu and the
  row's right-click menu. A new figure is a blank draw.io SVG attached as
  `figures/<id>.svg` and opened in draw.io; an existing draw.io figure opens
  as is. draw.io saves back into the same file. The action is disabled for
  plain SVGs and needs draw.io desktop, found via `MELLON_DRAWIO`, the
  standard Windows install folders or `PATH`.
- **Figures in PDF export.** draw.io desktop renders every attached figure
  (plain SVGs too) to a cropped vector PDF in one run. Each prints as a
  numbered figure, scaled down to fit. On an ordinary object it replaces the
  requirement text and the description is its caption (**Figure N:**
  *description*); on headings and tables it is captioned **Figure N** alone.
  Without draw.io, figures print the placeholder line as before.
  `export.pdf` takes the document `folder` and an `unrendered` out-list; MCP
  `export_document` adds `drawio_found: false` when draw.io is missing.
- **Installer CI workflow** (`.github/workflows/installer.yml`). It builds
  `Mellon-Setup-<version>.exe` with `packaging/build.ps1` on a Windows runner,
  installs it as a smoke test and keeps it as a run artifact. It runs on demand,
  or for a `v<version>` tag, which also attaches the installer and
  `SHA256SUMS.txt` to a draft GitHub release.

### Changed

- **Saving a figure in draw.io doesn't block editing.** On window
  activation, an in-place rewrite of the document's own figure files no
  longer raises the "Changed on disk" banner; the thumbnail and dirty marker
  refresh instead. Any other change still does.
- **draw.io figures render correctly in the grid and the full-size view:**
  draw.io's adaptive colours no longer paint black, its "Text is not SVG"
  notice is gone, labels in older draw.io files wrap instead of being cut to
  one line, and figures sit on white (legible in the dark theme).
- **PDF pages are plain white** instead of the off-white `#FAFAF7`: it prints
  cleanly, and figures (rendered on white) sit flush with the page.
- **Figure-only changes show up in review.** The commit dialog counts them as
  affecting their object, and object history (History tab, MCP
  `git_history`, `inspect_object`, `review_suspect_link`'s
  `commits_between`) includes commits that changed only the figure.

### Fixed

- **Multi-file operations are all or nothing.** Deleting an object that has
  incoming links, deleting an attribute or an enum option, acking several
  links: if a file can't be written partway, the files already changed are
  put back instead of leaving a half-applied change.
- draw.io's hidden `.$<name>.bkp` backup (and any `.$*` / `~$*` editor side
  file) is no longer committed or counted as a dirty file.
- The installer's TeX cache lacked `lmroman17-regular.otf`, which a document
  needs when a page break falls inside a section heading; such an export
  failed offline. The cache warm-up now fetches it, and exports the
  documents with their Traceability sections so the matrix's font is cached.

## 0.2.0 — not released on its own

The first release for team use. It ships a Windows installer with the GUI,
the MCP server and PDF export, plus the data-safety and git fixes needed when
several people work on one requirements repository.

### Added

- **Windows installer** `Mellon-Setup-<version>.exe`, built by
  `packaging/build.ps1` (PyInstaller onedir + Inno Setup). It installs per user
  into `%LOCALAPPDATA%\Programs\Mellon`, needs no admin rights, adds a
  Start-menu shortcut and does not change PATH. It ships:
  - `mellon.exe` (GUI) and `mellon-mcp.exe` (console MCP server)
  - `tools\tectonic.exe` 0.17.0 with a pre-warmed TeX cache in
    `tools\tectonic-cache`, so PDF export works offline
  - `LICENSE`, `THIRD-PARTY-NOTICES.txt` and `TEAM.md`
- **License:** GPL-3.0-or-later, © Tobias Hallberg (`LICENSE`, PEP 639
  metadata in `pyproject.toml`).
- **`--version`** on `mellon-gui` and `mellon-mcp`. The MCP server also reports
  Mellon's version as its server version.
- **`TEAM.md`:** the team routine. It covers setup, the daily pull, F5, edit,
  commit and push loop, rules that prevent conflicts, and a conflict playbook.
- **Documents that fail to load are reported, not hidden.** This covers, for
  example, a file left with merge-conflict markers.
  - The Project Explorer lists them greyed, with a warning icon. The tooltip
    and a double-click show `file:line`, the loader message and an
    "(unresolved merge conflict?)" hint.
  - MCP `list_documents` returns a `load_error` entry for each one.
    `validate_repo` and `repo_health` gain a `load_errors` list, and
    `validate_repo` returns `ok: false` while any document fails to load.
  - In an editor window, F5 on a document that no longer loads shows the real
    error.
- **Project Explorer, Menu → Refresh (F5):** re-reads every document from
  disk. Use it after a pull, or to re-check documents that failed to load.
- **Editor, Menu → "Discard uncommitted changes to this document…":** puts the
  document folder back to its last commit. Edits are reverted, objects added
  since the last commit are deleted, and other documents are not touched. It
  always asks first and has no shortcut. It is enabled only when the document
  is writable, has uncommitted changes and has been committed before.
- **GUI error log:** uncaught exceptions go to
  `%LOCALAPPDATA%\mellon\mellon\logs\mellon-gui.log` (or the temp folder if
  that location is unavailable) and are shown in a rate-limited dialog. The
  windowed exe has no console to print them to.
- **Figure placeholders in PDF export:** an attached figure prints
  "[Figure <file> not rendered in PDF export]". The MCP result lists those
  objects in `unrendered_figures`, and the GUI shows a notice.
- **CI** on GitHub-hosted runners (Ubuntu and Windows, with the fixture
  required via `MELLON_REQUIRE_FIXTURE=1`), plus a job that installs the wheel
  without the lockfile and smoke-tests it.

### Changed

- **Dependencies:**
  - `mcp>=1.27,<2`, without the `[cli]` extra. mcp 2.x removed
    `mcp.server.fastmcp`.
  - `pyside6-essentials>=6.11,<7` replaces the full `pyside6`.
  - `pygit2`, `lxml` and `jinja2` now have upper bounds.
- **`mellon-mcp --repo-root` is optional.** It defaults to
  `$CLAUDE_PROJECT_DIR`, then the current directory.
- **Repository root guard (GUI and MCP).** Both refuse a folder that:
  - is not in a git repository
  - is a subfolder of a clone
  - is a shallow clone
  - is a repository git cannot open

  `mellon-mcp` logs the reason and exits with status 2. The GUI explains the
  problem and what to open instead (for a subfolder, it names the clone's
  root).
- **Commits.** These rules apply to the GUI commit dialog, MCP
  `commit_changes` and `commit_message=`:
  - A commit is refused when git `user.name` / `user.email` are not set, and
    the error gives the `git config` commands. The silent `mellon@localhost`
    fallback is gone.
  - A commit is refused while a merge (or another git operation) is in
    progress. Finish it in your git client first.
  - Only the requested paths are committed, on top of HEAD. Files that another
    tool staged are never included. `commit_changes` without `paths` commits
    only uncommitted changes inside document folders.
- **The commit dialog's issue field** is prefilled with the document's
  *current* issue, or left empty when it has none.
  - Leaving the field unchanged makes a plain commit with no tag.
  - Raising it tags the commit `<doc-id>-issue-<n>`. A new issue must be
    higher than the current one, and MCP `tag_issue` enforces the same rule.
  - The manifest date is kept as it is, not set to today.
  - If the commit fails, the dialog's author/date edit is rolled back.
- **Suspect links follow git history simplification**, like
  `git log -1 -- <paths>`.
  - A merge commit counts as a change to an object only when the object's
    file (or its figure) differs from every parent.
  - Acks recorded by `add_link` use the same rule.
  - The suspect-scan cache is keyed by HEAD, so a commit refreshes it.
- **File writes are atomic** (temp file, then rename) and always use LF line
  endings.
- **Text input:**
  - `\x0b` and `\x0c` (line and page breaks pasted from Word or PDFs) become
    newlines.
  - CR / CRLF become LF.
  - Any other C0 control character except tab is refused, with an error
    naming it.
- **View and filter slugs** must match `^[a-z0-9]+(-[a-z0-9]+)*$`. Document
  ids and file names can't point outside their folder.
- **Deleting an object** also deletes its figure (`figures/<id>.svg`). Deletion
  is refused while any document in the repository fails to load, because
  links from that document can't be checked.
- **Delete in the GUI always asks for confirmation.** The per-window "Don't
  ask again" option is gone.
- **The stale banner blocks edits.** While "Changed on disk" is shown, edits
  are refused with "Changed on disk. Reload first (F5)."
- **Required attributes that are present but empty** count as missing
  (`ATTR_MISSING_REQUIRED`). An empty value clears an enum attribute.
- **PDF export:**
  - The cover page, running header and PDF title/author come from the
    document's metadata (title, author, date) and its current issue. The
    hard-coded "System Requirements Specification" / "ADAS Systems
    Engineering" text and the placeholder fields (project, classification,
    ASIL, standard) are gone. A field with no value is left off.
  - In the GUI, export runs in the background, so the window stays usable.
  - tectonic is looked up in this order: `MELLON_TECTONIC`, then
    `<exe-dir>/tools` (installed build), then `PATH`, then
    `<project-root>/tools` (source checkout).
  - A `tectonic-cache` folder next to the binary is used when
    `TECTONIC_CACHE_DIR` is unset.
  - tectonic runs with no console window, a 300 s timeout and UTF-8 output
    decoding.
- **MCP `export_document`:** `out_path` must end in `.pdf`, which is no longer
  appended silently, and must not be inside `.git`.
- **MCP `set_figure`:** a relative `source_path` resolves against the
  repository root.
- **MCP `new_object` / `update_object`** validate attributes and links before
  writing anything. Editor errors are reported as `EditorError`, not
  `OSError`.
- **MCP write tools report every file they touch.** This includes
  `delete_attribute`, `remove_attr_option`, and `create_document` with its
  `<doc-id>.rules.xml`, so `commit_message=` commits all of them.
  `--read-only` is also enforced inside the service.

### Fixed

- Picking an Inspector enum value with the mouse crashed the GUI.
- Outline shortcuts (`Ctrl+N`, `Ctrl+Shift+N`, `Ctrl+]`, `Ctrl+[`, `Alt+↑/↓`,
  `Del`) did nothing while the outline pane was hidden.
- Clicking into an empty grid cell and away wrote a value. Enum and boolean
  editors now have an empty entry, spin and date editors have an empty state,
  and an unchanged value writes nothing.
- Text typed in a grid cell or the Inspector but not yet committed was lost
  when an action ran (New, Delete, Link, Commit, …). It is now saved first.
- A failed commit left the commit dialog's author/date edit in the manifest.
- New objects and new link files were built as XML by hand, so some text
  produced invalid files (for example `&` in a link type).
- Path traversal (`../`) through view/filter slugs, figure ids and document
  ids. `clear_figure` now requires the object to exist.
- After a `--no-ff` merge, the merge commit counted as the latest change to
  every object it brought in. Links acked against the real change then read
  SUSPECT.
- Suspect state in the commit dialog, Inspector and grid gutter was stale
  after a commit. Editor windows now re-read link states on activation when
  HEAD has moved.
- Opening another repository left the old repository's editor windows open.
  Closed editor windows were not freed.
- Closing the Properties dialog discarded unapplied metadata edits without
  asking.
- Read-only mode could still write a view's column order when a column was
  dragged.
- Dragging a column border re-measured every row on each mouse move. The
  re-measure is now debounced.
- PDF export:
  - A table row starting with `[` or `*` broke the LaTeX build, and so did a
    heading, caption or table cell containing a blank line.
  - Export could hang (there was no timeout).
  - Export flashed a console window on Windows.
  - Export failed to decode tectonic's UTF-8 output.

### Upgrade notes

- **Re-ack once.** Links whose ack was recorded on a merge commit turn SUSPECT
  once after upgrading, because the current hash is now the commit that
  actually changed the target. Review them and re-ack. Squash and rebase merges
  still invalidate acks, because acks are commit hashes. Use merge commits
  (see `TEAM.md`).
- **Developer environments upgrading from 0.1** may need a clean Qt reinstall
  after the switch from `pyside6` to `pyside6-essentials`:

  ```
  uv sync --reinstall-package pyside6-essentials --reinstall-package shiboken6
  ```

- **Open the root of a full git clone.** The GUI and MCP now refuse non-git
  folders, subfolders of a clone and shallow clones, and `mellon-mcp` exits
  with status 2. Point `--repo-root` (or the folder you open) at the folder
  that contains `.git`. CI jobs that run Mellon need `fetch-depth: 0`.
- **Set a git identity** before committing:
  `git config --global user.name "Your Name"` and
  `git config --global user.email you@example.com`.
- **Issue tags are opt-in.** A plain commit no longer cuts a new issue. To
  release a new issue, raise the issue field in the commit dialog, then push
  the tag (`git push --tags`).
- **Every top-level folder `X` that contains `X/X.xml` is treated as a
  document.** A folder like that which isn't a valid Mellon document now
  shows as failed to load. `validate_repo` then returns `ok: false`, and
  deleting objects is refused. Rename or fix the folder.
- **Scripts and agents** that relied on `export_document` appending `.pdf`
  must pass a `.pdf` path.
- **Installer:** close Mellon and Claude Code (anything running
  `mellon-mcp.exe`) before installing a newer version; the installer refuses to
  start while either is running, leaving the existing install untouched. The
  exes are unsigned.
  When the installer is downloaded from the web, Windows SmartScreen may need
  "More info → Run anyway". Distributing it from an intranet share usually
  avoids that; Windows may still show an "Unknown Publisher" warning if the
  share isn't in the Local intranet zone.

## 0.1.0 — 2026-07-14

This version was never tagged; it corresponds to commit `8f636cb`. It was a
single-user baseline.

- **Storage:** each document is a folder of XML files in a git repository:
  - the manifest `<doc-id>/<doc-id>.xml`
  - one file per object in `objects/<id>.xml`
  - outgoing links in `links/<id>.xml`
  - optional SVG figures in `figures/<id>.svg`
  - saved views (`views/`) and filters (`filters/`)
  - `<doc-id>.rules.xml`
- **Library:**
  - loader, and a validator with structural checks plus a declarative rule
    engine
  - `editor.py` as the single place for all mutations
  - suspect-link detection and acknowledgement against git commit hashes
  - recursive trace, coverage report, substring search and the filter engine
  - a structured document diff (`mellon.diff.compare_document`, library only)
  - PDF export via LaTeX and tectonic
- **Attribute kinds:** `enum`, `text`, `integer`, `real`, `boolean`, `date`.
- **Tables:** structured tables on an object, with header rows, column
  alignment and merged cells. They are validated, edited in place in the GUI
  and exported as `longtable`.
- **Figures:** one SVG per object. Editing a figure makes child links suspect.
- **Issue tags:** each document has `<doc-id>-issue-<n>` lightweight git tags.
  The GUI commit dialog prefilled the next issue, so each commit cut a tag.
- **MCP server** `mellon-mcp --repo-root <path> [--read-only]` with 45 tools
  (13 read, 27 write, 5 git/review). Write tools take an optional
  `commit_message=` that auto-commits with a `Via: mellon-mcp` trailer.
- **GUI** `mellon-gui`:
  - a Project Explorer, plus a per-document editor with the outline, an
    editable grid with kind-aware editors, and Inspector and History tabs
  - link picker, trace view and suspect review
  - saved views and DOORS-style multi-rule filters
  - figures and inline table editing
  - commit dialog and PDF export
  - dark/light themes
  - "Changed on disk" detection with F5 reload
- **Install:** `uv tool install` puts `mellon-gui` / `mellon-mcp` on PATH.
  PDF export needs `tectonic`.

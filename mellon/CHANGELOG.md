# Changelog

All notable changes to Mellon. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- **Compare dialog.** Hamburger menu → **Compare…** shows what changed in a
  document between two versions: an issue, a branch, a commit, the latest
  commit or the working copy. It opens on the latest issue against the
  latest commit, lists added, deleted, modified and moved objects in outline
  order (an ack-only change is **Link reviewed**), and shows each changed
  field at both versions with changed words highlighted. Filters, swap,
  copy to clipboard, double-click to jump; works in read-only mode and
  compares in the background. Any version git's revision syntax names can
  be typed (`HEAD~2`, `@~2`, a tag, a hash prefix), and each result names the
  commit each version resolved to; a name that names more than one commit,
  such as a tag and a branch of the same name, is an error, and branches are
  listed by their full ref names. Comparing with the working copy first
  saves text still being typed; when the Document Editor can't save it (under
  the Changed on disk banner), the result says the pending edits aren't in the
  comparison. The History tab has **Compare with latest…**.
  Specified first, in `MELLON-VER-SRS`, Mellon's own requirements, kept in
  this repo next to its test cases in `MELLON-VER-STS`.
- **MCP `compare_document`** returns the same changes, and the full hash of
  the commit each version resolved to as `commit` (46 tools).
- `tests/test_mellon_spec.py`: fails when a test case in Mellon's own
  specification lists a pytest node id that names no test, so the test
  specification can't drift from the suite. It also checks that each
  requirement says exactly one of shall, should or could, that each Accepted
  or Implemented requirement has a test case, and that each document's rules
  catch a bare requirement or test case.
- **Mellon's own specification is split by area:** `MELLON-<AREA>-SRS` and
  `MELLON-<AREA>-STS` for STORE, EDIT, CHECK, TRACE, VER, PDF, MCP and GUI,
  with rules files. `docs/specification.md` says how they're written,
  `docs/definitions.md` holds the terms, and `docs/architecture.md` describes
  the design. Every area is specified: 1,675 requirements, of which 882 are
  built, and 769 test cases, with a test case for each built requirement.
  Tests written from the requirements alone found the defects fixed below;
  the decisions they need from a person are in `docs/open-questions.md`.
- `diff.compare_document` covers outline moves, tables, figures and the
  working copy (`WORKING_COPY`), counts a figure rewritten only in how its
  XML is written as no change, and gives each version's commit in full
  (`commit_a`, `commit_b`); new `document_versions`, `default_versions`,
  `word_diff`. Also `loader.parse_object_bytes` / `parse_links_bytes` /
  `parse_manifest_bytes` and `git_ops.issue_tags`.
- **`mellon-check`.** A pass/fail gate for CI: exit 0 when clean, 1 for any
  document that fails to load, validation error, or suspect or broken link,
  and 2 for a root it can't check, such as a shallow clone. It isn't in the
  installer. This repo's CI runs it, with `fetch-depth: 0`.
- **`review_suspect_link` says what changed.** New fields: `changed_fields`,
  `parent_attrs_at_ack`, `parent_attrs_now`, `parent_uncommitted`, `ack_found`
  and `commits_between_truncated`. Before, a parent change that touched only
  an attribute or a table showed identical text before and after.
- `git_ops.blob_id_at` and `DocumentService.uncommitted_document_paths`.
- This repo has a `.gitattributes` (`* text=auto eol=lf`), as `TEAM.md`
  recommends for repositories holding documents.
- `SCALING.md` and `benchmarks/`: measurements up to 5,000 objects per
  document, and the work that remains.
- **`.git/mellon-link-index.json`.** Answers from the suspect scan, kept so a
  new process needn't walk history again. It's a cache: deleting it is always
  safe, and `mellon-check` ignores it.
- **MCP `outline` takes `root` and `depth`.** `depth=1` lists only the top
  level, and `root` lists only the objects under one object. A row whose
  children were left out carries `descendants_omitted`. Without either
  argument, the response is unchanged.

### Changed

- **`diff.compare_document` parses each file as the loader does** and
  reports one it can't parse with an `error`, where it used to read it as an
  empty object. Results also carry `changes`, in display order, alongside
  the id-sorted `added_objects` / `removed_objects` / `modified_objects`.
- **Documents are meant for up to 1,000 objects,** counting headings and
  information text. Split a larger specification into components, each with
  its own set of documents. Nothing enforces it; `SCALING.md` explains the
  choice.
- **Much faster on large documents.** At 5,000 objects with 5,000 commits, a
  document used to not open within 30 minutes. See `SCALING.md` for numbers:
  - The suspect scan walks history once per target document, instead of once
    per link target (`link_index.py`).
  - Loading only re-parses files that changed.
  - Description, attribute and table edits update one row instead of resetting
    the grid, so scroll position and selection survive.
  - The History tab loads only while it is visible, and an edit no longer
    reloads it.
- **"N dirty" counts document files only,** in both status bars: what a
  commit of the documents would pick up. Other files in the repo, such as
  source code, no longer count.
- **MCP `suspects()` without a `doc_id` is refused while a document fails to
  load.** It used to leave that document out, so an empty answer could mean
  that nothing was scanned. Use `repo_health` for the partial picture.

### Fixed

- **The MCP server's log lines are whole whichever packages are installed.**
  When the Python package `rich` could be imported, the MCP library logged
  through it, wrapping each line at 80 columns, so the absolute path in a
  refusal or in the serving line was split over several lines. The server now
  logs through one plain handler of its own (`MELLON-MCP-SRS-10`, `-15`).
- **An object ID is no longer given out again after the highest-numbered
  object is deleted.** Before, the next new object took the deleted one's ID,
  so links on other branches, History, Compare and issued PDFs named a
  different object. The manifest now records the next object number as
  `next-object-number` on its `<document>` element: a new object takes the
  higher of it and one more than the highest object number present, and
  raises it; deleting leaves it. A value that isn't a whole number is
  `DOC_BAD_NEXT_NUMBER`. Resolving a merge keeps the higher of both sides'
  numbers and renumbers a clashing object from above both. A manifest without
  the number counts as 0, so existing documents need no change, and an older
  Mellon that doesn't write the number still works with this one, because the
  higher of the two is taken. Specified in `MELLON-STORE-SRS-1`, `-2`,
  `MELLON-EDIT-SRS-1`, `-2` and `MELLON-VER-SRS-64`, `-65`.
  `loader.parse_manifest_bytes` returns the number as a fifth item, and
  `Document.next_object_number` holds it.
- **Undo no longer gives out an undone object's ID again.** Undoing New
  object put the manifest back whole, next object number included, so the
  next new object took the undone one's ID, and a link made to it meanwhile
  named the new object. Undo and redo now keep the higher of the number on
  disk and the number the step had (`MELLON-EDIT-SRS-346`); undoing several
  creates in a row still works.
- `git_ops.log_for_path(since_commit=…)` now matches `git log <since>..HEAD`.
  On a merged history it could drop commits from the other side of a merge,
  and when the commit was no longer on HEAD's history (after a rebase) it
  reported everything back to the root. `review_suspect_link` and
  `DocumentService` history both use it.
- `git_ops.dirty_paths_under` matches a path equal to its argument, as its
  docstring said, not only paths below it.
- `DocumentService.ack_all` raises `ServiceError` for a links file that became
  malformed after the scan, instead of a raw XML error.
- **A date is valid only as YYYY-MM-DD.** On Python 3.11 and later, validation
  also took other ISO 8601 forms, such as `20260210` and the week date
  `2026-W06-2`, as a date attribute value or the manifest's metadata date.
  They are now `ATTR_BAD_DATE` and `META_BAD_DATE`, from one check
  (`MELLON-CHECK-SRS-21`, `-42`). A document holding such a date reports it
  after upgrading.
- **A rule condition `name=""` no longer matches an object on which the
  attribute isn't set.** An attribute that isn't set has no value, so it
  matches no value; one set to an empty value still matches `""`. Before,
  such a rule also applied to every object without the attribute, and
  reported `RULE_REQUIRED_MISSING` or `RULE_FORBIDDEN_PRESENT` for them. The
  same holds for `requirement-type` (`MELLON-CHECK-SRS-54`, `-55`). The
  Inspector's required and forbidden markers follow the same rule.
- **`mellon-check` prints a broken link once.** A link to a missing object of
  its own document printed two lines, `LINK_TARGET_MISSING` and `BROKEN`, and
  counted two problems. It now prints only the `LINK_TARGET_MISSING` line. A
  broken link into another document still prints its `BROKEN` line
  (`MELLON-CHECK-SRS-134`).
- **Exported PDFs number their pages from the cover.** The cover and the
  contents page both said Page 1, the total was one short (7 pages printed
  "of 6"), and the contents gave each section's page one too low. The page
  after the cover is now page 2 (`MELLON-PDF-SRS-104`).
- **A manifest's name must match its folder's in letter case too.** On
  Windows and macOS, `DOC/doc.xml` made `DOC` a document; it now doesn't, as
  on Linux, so every system sees the same documents (`MELLON-STORE-SRS-18`).
- **A load error after an edit or in the Commit dialog says "(unresolved
  merge conflict?)"** when the file holds git conflict markers, as F5 already
  did (`MELLON-STORE-SRS-37`).
- **A boolean attribute set to `True` or `FALSE` is written as `true` or
  `false`** (`MELLON-STORE-SRS-97`).
- **A figure must be an SVG in SVG's namespace.** An `<svg>` root without
  `xmlns="http://www.w3.org/2000/svg"` was accepted and written; it is now
  refused, with nothing written (`MELLON-STORE-SRS-131`).
- **A view's or filter's file name turns only A–Z into lower case.** A name
  with the Kelvin sign or the dotted capital I got a file name with `k` or
  `i` (`MELLON-STORE-SRS-153`).
- **When a commit from the Commit dialog fails, the manifest is put back with
  an atomic write.** A failure partway through the put-back could leave the
  manifest cut short, and its error escaped as an unexpected error; the
  manifest now stays whole and the Document Editor says it wasn't put back
  (`MELLON-STORE-SRS-167`).
- **The MCP server's read-only refusal and a missing document come back as a
  failure result.** A write tool on a read-only server, and a write tool
  given a document that doesn't exist, returned a tool error with only a text,
  so an agent couldn't branch on them. They now return `ok: false`, `error`
  and `error_type`: `PermissionError` for the read-only refusal
  (`commit_changes` and `export_document` included), `EditorError` for the
  missing document (`MELLON-MCP-SRS-45`, `-53`, `-100`).
- **`mellon-mcp --log-level` takes effect.** Every level wrote the same lines;
  `WARNING` and `ERROR` now leave out the informational ones, the MCP
  library's own included (`MELLON-MCP-SRS-120`).
- **A repository whose `.git/HEAD` git can't read is refused** by the MCP
  server, `mellon-check` and the GUI, as git itself refuses it. It was served
  (`MELLON-MCP-SRS-8`, `MELLON-STORE-SRS-9`).
- **`commit_changes` takes a document folder as a path.** It failed with "it
  is a directory"; a folder now selects each changed file under it, and a
  deleted folder commits the deletion instead of answering "nothing to
  commit". An editor side file named as a path is left out of the commit
  (`MELLON-VER-SRS-91`, `-92`).
- **Mellon refuses to commit while a cherry-pick of several commits with
  `--no-commit` is stopped,** which `git status` reports as in progress
  (`MELLON-VER-SRS-118`).
- **A refused commit puts the manifest's author and date back** when another
  git program committed while the Commit dialog's commit was being recorded
  (`MELLON-VER-SRS-168`).
- **Discard is refused when the latest commit holds a submodule or a symbolic
  link in the document's folder,** changed or not, and changes nothing
  (`MELLON-VER-SRS-209`).
- **Resolving a merge again after a resolve failed partway stages the merged
  manifest,** so the merge commit is the same as after a resolve that never
  failed (`MELLON-VER-SRS-366`).
- **An enumeration attribute with no options is refused,** by
  `define_attribute`, the library and the Add attribute dialog. It used to be
  written, and left a document that didn't load (`MELLON-EDIT-SRS-65`).
- **The Properties dialog's Attributes tab makes an attribute required or
  optional,** with a check box in the Required column (`MELLON-EDIT-SRS-92`).
- **The Inspector's enumeration field offers an empty entry,** so a value can
  be cleared, and a boolean attribute is a choice of empty, true and false
  instead of free text (`MELLON-EDIT-SRS-130`, `-131`).
- **The Inspector refuses an integer, real or date that doesn't fit the
  attribute's kind** instead of saving it (`MELLON-EDIT-SRS-132` to `-134`).
- **The grid's integer, real and date editors can be emptied,** save a value
  exactly as typed and refuse one that doesn't fit. Dates from 0001-01-01 to
  9999-12-31 can be entered. They used to round or reformat what was typed,
  and saved today's date on Enter in an empty cell (`MELLON-EDIT-SRS-132` to
  `-134`, `-363`).
- **Choosing a value from the grid's enumeration or boolean list saves it at
  once,** as the Inspector does (`MELLON-EDIT-SRS-135`).
- **Copying an object copies its table too** (`MELLON-EDIT-SRS-145`).
- **`move_object` with `before` or `after` an object in another section,** and
  no `parent_id`, moves the object directly before or after that object
  instead of refusing (`MELLON-EDIT-SRS-187`).
- **A filter using is-not-empty on a boolean attribute is refused,** like any
  other operator a boolean doesn't take (`MELLON-EDIT-SRS-295`).
- **Enter in a table cell inserts a line break** (`MELLON-EDIT-SRS-211`), and
  **Ctrl+Enter saves the cell's text** to the object and closes the cell's
  editor (`MELLON-GUI-SRS-165`).
- **A figure that draw.io saves as an empty or incomplete PDF prints as a
  placeholder** and is listed in the notice of figures not drawn, and in
  `export_document`'s `unrendered_figures`, instead of failing the whole
  export (`MELLON-PDF-SRS-58`, `-61`, `-73`).
- **A document without a title no longer shows its ID as the title** on the
  cover and in the page headers; the title is left out (`MELLON-PDF-SRS-82`).
- **A table's leading header rows repeat on each page** the table continues
  onto, even when a later header row follows a body row
  (`MELLON-PDF-SRS-147`).
- **`export_document`'s `suspect_links` carry each link's `doc_id`,** the
  same fields the `suspects` tool gives (`MELLON-PDF-SRS-54`).
- **A Document Editor no longer writes over a change another program made**
  (an agent over MCP, a git client, draw.io) while the window wasn't
  activated. Each edit and each commit checks the files first and is refused
  with the Changed on disk banner, also when the change was made while the
  Commit dialog was open. Typed text stays in its field. A file saved again
  with the same content no longer counts as changed (`MELLON-GUI-SRS-109`,
  `-111`, `MELLON-STORE-SRS-182`).
- **Lock and backup files other programs leave in a document folder** (names
  starting with `~$` or `.$`) no longer bring up the Changed on disk banner
  (`MELLON-GUI-SRS-99`).
- **While the Changed on disk banner is up,** dragging a column of the active
  view no longer saves the view, and editing or pasting in a grid cell says
  to reload (F5) instead of doing nothing (`MELLON-GUI-SRS-104`).
- **Trace, Validate, Discard uncommitted changes and Read-only save the text
  being typed first.** Read-only no longer throws it away, and Discard saves
  it before asking (`MELLON-GUI-SRS-74`).
- **Ctrl+W closes a Document Editor on Windows,** as Ctrl+F4 does
  (`MELLON-GUI-SRS-164`).
- **The GUI's error log goes where INSTALL.md says:**
  `%LOCALAPPDATA%\mellon\mellon\logs\mellon-gui.log`, or
  `%TEMP%\logs\mellon-gui.log` when `%LOCALAPPDATA%` is unset or isn't a
  folder (`MELLON-GUI-SRS-21`, `-22`).
- **Double-clicking an outgoing link in the Inspector, or Jump, selects the
  target in its own document's Document Editor,** opening it if needed; the
  window the jump started from no longer switches documents
  (`MELLON-TRACE-SRS-100`).
- **A link to an object that isn't in the working copy is refused** by the
  library's `add_link`, the document service, MCP `add_link` and
  `update_object`, and the Document Editor's Link…, with "link target
  <doc>/<id> does not exist in the working copy" (naming a missing document
  when the whole document is gone). It used to be created and then read
  BROKEN. The link picker no longer offers an object whose file is gone, and
  copying an object with its links is refused before anything is written
  when one of its links is broken (`MELLON-TRACE-SRS-6`).
- **A link to an object that exists but was never committed reads SUSPECT,
  not BROKEN,** in the scan, `mellon-check`, the state gutter and MCP
  `suspects`; this includes every link in a repository with no commit. It
  still can't be acknowledged until the target is committed, and
  `ack_suspects` skips it with that reason (`MELLON-TRACE-SRS-45`, `-47`).

- **`mellon-check` prints a link outside the document and its parents once.**
  A link to a document that is neither the link's own document nor a parent,
  whose target object doesn't exist, was printed as `LINK_NOT_ALLOWED` and
  again as a `BROKEN` link, and counted as two problems. It now gets the
  `LINK_NOT_ALLOWED` line alone (`MELLON-CHECK-SRS-134`, `-33`).
- **Every load error names its file and line.** A manifest whose enum
  attribute has no option, or whose parent, attribute or ref element lacks a
  required XML attribute, gave no file; and only an XML syntax error gave a
  line. Each load error now names the file and the line of the element it
  concerns, in `DocLoadError`, MCP `list_documents`, `validate_repo` and
  `repo_health`, and in `mellon-check`, whose line now reads
  `<doc>: LOAD_ERROR <file>:<line>: <reason>` with the file relative to the
  repository root (`MELLON-STORE-SRS-29`, `MELLON-CHECK-SRS-90`, `-102`,
  `-137`).

- **Coverage counts each object once.** An outline that named an object
  twice counted it twice, and an object the outline left out wasn't counted,
  in `repo_health`'s coverage rows, MCP `coverage_report` and the PDF. Each
  object of the document is now counted once, in outline order and then the
  objects the outline leaves out (`MELLON-CHECK-SRS-107`, `-108`).
- **`repo_health` no longer reports 0 suspect links when it couldn't look.**
  When the suspect scan failed (no git repository, or `.git` moved after the
  server started), each suspect and broken count read 0. They are now
  `null`, and the new `suspect_scan_error` says why (`MELLON-CHECK-SRS-97`,
  `-101`). A failing coverage row is no longer silently left out either.

- **The Validate action reads the working copy.** A Document Editor
  validated the copy it had read earlier, so after a pull left conflict
  markers in the document it said "validation passed". It now reads the
  document's files when the action is chosen and shows the load error
  instead (`MELLON-CHECK-SRS-2`).
- **The Validate action lists every validation error,** in a scrollable
  list, where it used to show the first ten and "… and N more"
  (`MELLON-CHECK-SRS-79`).

- **`mellon-check` reads the repository once.** It used to read it three
  times (to validate, to list, to scan); a file that changed in between,
  such as during a pull, could leave a document validated clean and then
  skipped by the scan without a word, so the check could pass with a load
  error and suspect links. Each document is now validated and scanned from
  the one reading, or reported as a load error (`MELLON-CHECK-SRS-147`,
  `-149`).

- **A table whose cell spans over a row span from above is a load error.**
  A cell whose column span crossed a column still covered by a row span from
  an earlier row was accepted: one grid position was covered twice and one
  left empty, and the grid, the table editor and the PDF drew it by guessing.
  The loader, the editor and validation now report it
  (`MELLON-STORE-SRS-111`, `-113`).
- **The merge-conflict note needs all three conflict markers.** A load error
  was labelled "(unresolved merge conflict?)" for a file holding only a
  `<<<<<<<` line. The note is now added only when the file holds a
  `<<<<<<<` line, a later `=======` line and a later `>>>>>>>` line, as the
  definition of conflict markers says (`MELLON-STORE-SRS-30`).

- **Text after an element or comment inside a description is read.** In
  `<description>The pump shall <b>always</b> stop.</description>` (a
  hand-written file, another tool or a newer Mellon) only "The pump shall"
  was read, and setting a new description kept the child element and the
  text after it in the file. An element the format doesn't define, or a
  comment, is now read as if absent and the text around it is kept; each
  text Mellon writes replaces the element's whole content
  (`MELLON-STORE-SRS-46`, `-84`, `-87`).
- **An empty value is written as `<x/>` every time.** An attribute,
  description, title or metadata field set to an empty value was written as
  `<x></x>`, and the next unrelated rewrite of the file turned it into
  `<x/>`: a change nobody made, which moved the object's last commit and
  turned links to it suspect. Mellon now writes the one form `<x/>`
  (`MELLON-STORE-SRS-208`).

- **Acknowledging a link writes the links file with 2-space indentation.**
  A links file indented another way kept that indentation when a link in it
  was acknowledged, so the next edit re-indented it in a later commit
  (`MELLON-STORE-SRS-190`).
- **A figure is written whole or not at all.** Attaching or replacing a
  figure, and copying an object with its figure, wrote straight into the
  figure file, so a stop or a full disk part-way left a truncated SVG and
  lost the draw.io diagram in it; a copy's figure also wasn't undone when
  the copy failed. Figures now go through a temporary file that replaces
  the old one, and the copy is journaled (`MELLON-STORE-SRS-167`).

- **A filter rule without an operator is a load error, and isn't written.**
  An `attr` rule with no `op`, or an empty one, loaded and then matched
  nothing; `create_filter` and `set_filter_rules` wrote such a rule when
  given one (`MELLON-STORE-SRS-145`, `-148`).
- **Dates are written as YYYY-MM-DD only.** The manifest date and date
  attribute values set through the composite MCP tools took any form
  Python's date parser reads, such as `20261113` and the week date
  `2026-W46-5`, which validation then reported as `META_BAD_DATE` or
  `ATTR_BAD_DATE`. They now take the same check validation uses
  (`MELLON-STORE-SRS-64`, `-98`).
- **A control character in a view's column name, a filter rule's attribute
  name or a parent document ID is named** in the refusal, as in the other
  stored text, instead of lxml's general message (`MELLON-STORE-SRS-214`).

- **A link without a type, and a parent that isn't a document ID, are
  refused before anything is written.** MCP `add_link` with an empty
  `link_type`, or `add_parent` with an empty or impossible ID, wrote a file
  the loader then rejected, so the whole document stopped loading while the
  call reported failure. And when a document failed to load again after an
  edit, the GUI's cached document list dropped it without a load error until
  a full refresh; the next listing now reads the repository again and shows
  the load error (`MELLON-STORE-SRS-27`, `-68`, `-120`).

- **Windows and Linux read the same object, figure, view and filter files.**
  On Windows `objects/DOC-7.XML` was read as object DOC-7 and
  `figures/DOC-0.SVG` as DOC-0's figure; on Linux CI neither was, so the
  same commit checked differently. The `.xml` and `.svg` suffixes now count
  letter case everywhere, as the manifest's name already did
  (`MELLON-STORE-SRS-81`, `-129`).
- **Opening a bare repository is refused as outside a work tree.** It was
  called a subfolder of a work tree rooted in the folder above it, and the
  GUI offered to open that folder, which then wasn't a git repository at
  all (`MELLON-STORE-SRS-7`, `-12`).

### Security

- **A link's target can no longer make Mellon look outside the repository.**
  A links file from another clone could set `target-doc` or `target-id` to a
  path such as `../x` or, on Windows, a share such as `\\host\share`. Working
  out the link's state, creating the link, or reviewing it with
  `review_suspect_link` then looked that path up. For a share, that means
  Windows offers the user's credentials to the other host. Now a target that
  isn't an object ID (`<document ID>-<n>`) is a broken link, worked out from
  the text alone. A document or file name given to the MCP server or the GUI
  with a path separator or a drive is refused the same way, before it is
  looked up. Specified in `MELLON-TRACE-SRS-77` and `-79`; new
  `schema.is_document_id` and `schema.is_object_id`.

### Removed

- `git_ops.dirty_count`: nothing uses it now.

### Upgrade notes

- **Upgrade everyone who creates objects.** Mellon 0.3.0 keeps
  `next-object-number` but doesn't use or raise it, so an object created with
  0.3.0 can still take the ID of a deleted one. Documents need no change.

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

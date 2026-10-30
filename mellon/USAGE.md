# Usage

This page covers Mellon 0.3.0. Mellon works on **documents**, which are folders inside a git repository. You can work with them in three ways, and all three use the on-disk format described below:

- **GUI:** `mellon.exe` from the Windows installer, or `mellon-gui` in a dev environment.
- **MCP stdio server:** `mellon-mcp`.
- **Python library.**

The GUI and the MCP server both go through the same `DocumentService` façade.

Related pages:

- [INSTALL.md](INSTALL.md): installer, dev setup, and MCP client configuration.
- [TEAM.md](TEAM.md): the team routine of pull, F5, edit, commit, and push.
- [STATUS.md](STATUS.md): what's built and the file map.

## Repository requirements

| Requirement | What happens otherwise |
|---|---|
| The repo root is the **root of a git work tree**, meaning the folder that contains `.git`. | A folder outside git, or a subfolder of a clone, is refused. The GUI shows a "Could not open repository" box. `mellon-mcp` logs the reason and exits with status 2. |
| The clone is **full**, not shallow. | Refused, because suspect detection needs the full history and every link would otherwise read SUSPECT. Fix it with `git fetch --unshallow`. CI checkouts need `fetch-depth: 0`. |
| git `user.name` and `user.email` are set. | Every commit is refused, from both the GUI and MCP. Set them once: `git config --global user.name "Your Name"` and `git config --global user.email you@example.com`. |
| Every top-level folder `X` that contains `X/X.xml` is a valid Mellon document. | The folder is reported as a load error (see [Documents that fail to load](#documents-that-fail-to-load)). Folders whose names start with `.` are skipped. |

Documents sit directly under the repo root; nested document folders are not supported. Mellon commits but never pulls, pushes, or merges. Sync with an external git client as described in [TEAM.md](TEAM.md).

Mellon is designed for documents of up to about **500 objects**.

## On-disk format

### Repository layout

```
my-project/                      <-- git work-tree root
├── .git/
├── VCU3-SyRS/                   <-- parent document
│   ├── VCU3-SyRS.xml                manifest
│   └── objects/
│       └── VCU3-SyRS-42.xml
└── VCU3-SRS/                    <-- child document (folder name = manifest id)
    ├── VCU3-SRS.xml                 manifest
    ├── VCU3-SRS.rules.xml           conditional validation rules (optional)
    ├── objects/                     one file per object
    │   ├── VCU3-SRS-0.xml
    │   └── VCU3-SRS-1.xml
    ├── links/                       outgoing links, one file per source object (optional)
    │   └── VCU3-SRS-1.xml
    ├── figures/                     at most one SVG per object (optional)
    │   └── VCU3-SRS-1.svg
    ├── views/                       saved column layouts (optional)
    │   └── dev.xml
    └── filters/                     saved filters (optional)
        └── unverified-safety.xml
```

Naming rules (the loader enforces these):

- **Document folder:** its name equals the manifest's `<document id>`, and the manifest is `<folder>/<folder>.xml`.
- **Object file:** its stem equals its `<object id>`, so `VCU3-SRS-1.xml` holds `<object id="VCU3-SRS-1">`.
- **Links file:** its stem equals its `<links source>`.
- **View and filter files:** the stem is the slug. Mellon derives the slug from the name: lowercase, with every run of other characters turned into `-`, and a `-2`, `-3`, … suffix on collision. Rename, delete, and edit refuse any slug that doesn't match `[a-z0-9]+(-[a-z0-9]+)*`.
- **Duplicates:** a duplicate object id, links source, view slug, or filter slug in one document is a load error.

### Manifest: `<doc-id>.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<document id="VCU3-SRS">
  <metadata>
    <title>VCU3 Software Requirements Specification</title>
    <author>A. Engineer</author>
    <date>2026-04-21</date>
  </metadata>
  <parents>
    <parent doc="VCU3-SyRS"/>
  </parents>
  <attributes>
    <attribute name="type" label="Type" kind="enum" required="true">
      <option>Heading</option>
      <option>Requirement</option>
      <option>Information</option>
      <option>Note</option>
    </attribute>
    <attribute name="req-type" label="Requirement Type" kind="enum" required="false">
      <option>Functional</option>
      <option>Safety</option>
    </attribute>
    <attribute name="status" label="Status" kind="enum" required="true">
      <option>Draft</option>
      <option>Approved</option>
    </attribute>
    <attribute name="rationale" label="Rationale" kind="text" required="false"/>
    <attribute name="max-latency-ms" label="Max latency (ms)" kind="integer" required="false"/>
  </attributes>
  <outline>
    <ref id="VCU3-SRS-0">
      <ref id="VCU3-SRS-1"/>
      <ref id="VCU3-SRS-2"/>
    </ref>
    <ref id="VCU3-SRS-3"/>
  </outline>
</document>
```

| Element | Content |
|---|---|
| `<document id>` | Root element. `id` must equal the folder name. |
| `<metadata>` | `<title>`, `<author>`, and `<date>`, all optional. `date` is ISO `YYYY-MM-DD`; anything else is `META_BAD_DATE`. The PDF cover and running header use these fields. The document's issue is **not** stored here (see [Issue tags](#issue-tags)), and a leftover `<issue>` element is ignored. |
| `<parents>` | One `<parent doc="..."/>` per upstream document. Links may target only this document or a declared parent. The tools refuse anything else, and the validator reports it as `LINK_NOT_ALLOWED`. |
| `<attributes>` | The attribute schema. Each `<attribute>` has a required `name` and `kind`, an optional `required` (`true`/`false`, default `false`), and an optional `label` (display name, which falls back to `name`; `type` defaults to "Type"). `kind` is one of `enum`, `text`, `integer`, `real`, `boolean`, `date`. An `enum` must list at least one `<option>`, and other kinds must list none. Tools accept attribute names of letters, digits, `-`, and `_`. |
| `<outline>` | The object hierarchy and document order, as nested `<ref id>` elements. Every object appears exactly once; violations are `OUTLINE_DUP`, `OUTLINE_MISSING_OBJECT`, and `OBJECT_NOT_IN_OUTLINE`. |

A document created from the GUI, or by MCP `create_document` without an `attr_schema`, gets a default schema and a rules file:

- **Schema:** `type` (required enum: `Heading`, `Information`, `Note`) and `rationale` (text).
- **Rules file:** forbids `rationale` on Headings.

### Objects: `objects/<id>.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<object id="VCU3-SRS-1">
  <attr name="type">Requirement</attr>
  <attr name="status">Draft</attr>
  <attr name="rationale">Required by system-level safety case SSA-014.</attr>
  <description>The VCU shall generate a FAULT_OVERTEMP signal within 200 ms of the coolant temperature exceeding 106 degC.</description>
</object>
```

- **`<attr name="...">value</attr>`:** one element per attribute value.
  - A missing `<attr>` and an empty one both mean "no value".
  - Every name must be declared in the manifest (`ATTR_UNKNOWN`).
  - The same name twice is a load error.
- **`<description>`:** the object text. It is plain text, and line breaks are kept.
- **Object ids:** new objects get `<doc-id>-<N>`, where N is one higher than the highest number in use on your branch. Two branches can take the same N; when they are merged, the later one is renumbered (see [Merging branches](#merging-branches)).
- **Object type:** `Heading` is a structural type. The GUI, rules, coverage, and export treat it specially.

#### Tables

An object can carry a `<table>` after its `<description>`. By convention its `type` is `Table` and the description is the caption, but tooling keys off the element's presence, not the convention.

```xml
<table>
  <columns>
    <col align="left"/>      <!-- align: left | center | right (default left) -->
    <col align="right"/>
  </columns>
  <row header="true">        <!-- header rows render bold/shaded, above the rule -->
    <cell>Signal</cell>
    <cell>Max latency</cell>
  </row>
  <row>
    <cell colspan="2">Applies to all faults</cell>   <!-- colspan/rowspan written only when > 1 -->
  </row>
</table>
```

- **Width:** the grid is as wide as the number of `<col>` elements.
- **Spans:** cells are plain text. A covered slot has no `<cell>` of its own, because the spanning cell owns it.
- **Tiling:** the rows must tile the grid exactly. Every position is covered once, nothing overflows, and no rowspan runs past the last row. The loader rejects a table that breaks this.
- **Not linkable:** cells can't be linked one by one. This is deliberately not the DOORS cells-as-objects model.
- **Export:** a table renders as a `longtable`. Header rows at the top of the table repeat on every page it spans, unless a header cell's row span reaches into the body.

### Links: `links/<id>.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<links source="VCU3-SRS-1">
  <link target-doc="VCU3-SyRS" target-id="VCU3-SyRS-42" type="satisfies" ack="0c2f497"/>
</links>
```

- **Outgoing only:** the file holds the outgoing links of object `source`. Incoming links are never stored; Mellon computes them by scanning every document in the repo.
- **File lifecycle:** an object with no outgoing links has no file, and Mellon deletes the file when its last link is removed.
- **Required attributes:** `target-doc`, `target-id`, and `type`.
  - `type` is free text. The GUI offers `satisfies`, `verifies`, `refines`, and `implements`.
  - `target-doc` is this document or a declared parent, which must be its own folder at the repo root.
- **`ack`:** optional. It is the short (7-character) hash of the target's commit that was last reviewed for this link.
  - A new link gets the target's current hash when the target is committed.
  - Otherwise `ack` is left out and the link reads SUSPECT ("never reviewed").
  - See [the suspect-link workflow](#the-suspect-link-workflow).

### Figures: `figures/<id>.svg`

- **One per object:** at most one figure, attached to object `<id>`. The figure file existing is the only marker; the object XML doesn't change.
- **SVG only:** the root must be `<svg>`.
- **draw.io figures:** an SVG saved by draw.io carries the editable diagram in its root `content` attribute, so the same file is both the picture and the draw.io source. Figures made with the GUI's **New draw.io figure** are always of this kind, and so is a draw.io `.drawio.svg` export. draw.io saves back into `figures/<id>.svg`.
  - draw.io writes a hidden `.$<id>.svg.bkp` backup beside each figure it saves. Mellon never commits these side files (`.$*`, `~$*`) and leaves them out of the dirty count; see [TEAM.md](TEAM.md) for ignoring them in your git client.
- **Suspect links:** editing a figure flips links to its object to SUSPECT, just as editing the object file does. The figure's commits show in the object's history.
- **Deletion:** deleting the object deletes its figure.
- **PDF export:** figures are drawn by draw.io desktop as vector graphics and numbered. The caption is the object's description; see **PDF export (Ctrl+E)** under [GUI](#gui-mellonexe-installer-or-mellon-gui-path-dev). A diagram with several pages exports its first page.

### Views: `views/<slug>.xml`

A view is a saved set of visible attribute columns in the grid, in display order.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<view name="Dev">
  <attribute name="req-type"/>
  <attribute name="rationale"/>
</view>
```

The system columns (state, id, and description) are always shown. MCP manages views with `manage_view(action="create"|"rename"|"delete"|"edit", …)`.

### Filters and rules

- **Saved filters:** `filters/<slug>.xml`. See [Saved filters](#saved-filters-doc-idfiltersslugxml).
- **Conditional validation rules:** `<doc-id>.rules.xml`. See [Validation rules](#validation-rules-doc-idrulesxml).

### Writes and formatting

- **Atomic writes:** every write goes to a temporary sibling file that then replaces the target, so a failed write never leaves a truncated file. On Windows the replace is retried briefly when another program holds the file.
- **Fixed file format:** files are written as UTF-8 with LF line endings, the header `<?xml version="1.0" encoding="UTF-8"?>`, 2-space indentation (`lxml.etree.indent`), and a trailing newline. A repo-wide `.gitattributes` with `* text=auto eol=lf` keeps git clients from converting line endings (see [TEAM.md](TEAM.md)).
- **One-time reformat:** the first edit of a hand-authored file re-indents it. Decorative blank lines between manifest sections are collapsed; this is a one-time diff, and later edits are stable.
- **Text cleanup:** CRLF and CR become LF. The vertical tab `\x0b` (Word's manual line break) and the form feed `\x0c` (page break) become newlines. Any other control character except tab is refused with an error that names it, because XML 1.0 can't store it.

## Schema kinds

Every attribute kind is stored as a canonical string, and `validate` checks the non-string kinds:

| Kind | Canonical form | Validation error code |
|---|---|---|
| `enum` | one of the declared `<option>`s | `ATTR_BAD_ENUM` |
| `integer` | base-10 signed digits, e.g. `42` or `-7` | `ATTR_BAD_INTEGER` |
| `real` | parseable float, e.g. `3.14` | `ATTR_BAD_REAL` |
| `boolean` | exactly `true` or `false` (lowercase) | `ATTR_BAD_BOOLEAN` |
| `date` | ISO 8601 `YYYY-MM-DD` | `ATTR_BAD_DATE` |

Empty values skip the type checks. A required attribute that is missing **or empty** (for example an enum cleared in the GUI) is reported once, as `ATTR_MISSING_REQUIRED`.

## Validation rules (`<doc-id>.rules.xml`)

A document can have a sibling `<doc-id>.rules.xml` file that adds conditional checks on top of the schema. Rules are loaded automatically and run as part of validation.

```xml
<rules>
  <rule>
    <when type="Heading"/>
    <then><attr name="rationale" status="forbidden"/></then>
  </rule>
  <rule>
    <when status="Approved"/>
    <then><attr name="rationale" status="required"/></then>
  </rule>
  <rule>
    <when type="Requirement" req-type="Functional"/>
    <then>
      <attr name="verification-method" status="required"/>
      <link target-doc="VCU3-SyRS" status="required"/>
    </then>
  </rule>
</rules>
```

**`<when>` predicates.** All attributes on `<when>` must match (AND), and an empty `<when>` matches every object. Three names are reserved:

| Reserved name | Matches |
|---|---|
| `type` | The `type` attribute. |
| `requirement-type` | The attribute named `RequirementType`. |
| `has-outgoing-link` | `true` or `false`, depending on whether the object has outgoing links. |

Any other name matches the object's attribute of that name by equality. For example, `<when status="Approved"/>` matches objects whose `status` is `Approved`.

**`<then>` directives:**

- `<attr name="X" status="required|optional|forbidden"/>` reports `RULE_REQUIRED_MISSING` or `RULE_FORBIDDEN_PRESENT`.
- `<link target-doc="DOC-ID" status="required"/>` requires the object to have at least one outgoing link to `DOC-ID`. `status` defaults to `required`, which is the only value. A violation is reported as `RULE_LINK_REQUIRED_MISSING`.

When several rules match the same object, attribute directives merge in file order, with later rules overriding earlier ones for the same attribute. Link directives accumulate.

## Saved filters (`<doc-id>/filters/<slug>.xml`)

A filter narrows the objects shown in the grid, using rules on attribute values and on whether links are present, much like a DOORS filter.

- **Scope:** filters belong to one document, are version-controlled, and are stored separately from `views/`.
- **Independent of views:** the grid combines any view with any filter. Activating a filter doesn't change the columns, and switching views doesn't change the active filter.
- **Editing:** filters are created and edited in the GUI (Ctrl+Shift+F) or through the library. MCP can only read them, via `show_document(..., include=["filters"])`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<filter name="Unverified safety requirements" combinator="all" keep-ancestors="true">
  <rule kind="attr" op="eq" attr="req-type" value="Safety"/>
  <rule kind="attr" op="ne" attr="status" value="Approved"/>
  <rule kind="has-outgoing" op="present" attr="VCU3-SyRS"/>
  <rule kind="has-incoming" op="absent" attr="VCU3-STS"/>
</filter>
```

This filter matches every Safety requirement that isn't Approved, satisfies something in VCU3-SyRS, and is not yet verified by any VCU3-STS test.

| Attribute on `<filter>` | Meaning |
|---|---|
| `name` | Required. The name shown in the GUI menu and pill. |
| `combinator` | `all` (AND) or `any` (OR) over the rules. Default `all`. |
| `keep-ancestors` | `true` adds each matched object's outline ancestors back to the visible set, so that heading context survives the filter (the DOORS default). Default `true`. |

Each `<rule>` is one of three kinds:

| `kind` | Other attributes | Meaning |
|---|---|---|
| `attr` | `attr` (required, the attribute name), `op`, `value` | Attribute predicate. `op` must be valid for the attribute's kind (table below). |
| `has-outgoing` | `attr` (optional target doc id; empty means any), `op` (`present` / `absent`) | True if the object has at least one outgoing link to that doc. |
| `has-incoming` | `attr` (optional source doc id; empty means any), `op` (`present` / `absent`) | True if at least one link, from this document or another, targets the object. |

Operators per attribute kind:

| Kind | Valid `op` values |
|---|---|
| `enum` | `eq`, `ne`, `is-empty`, `is-not-empty` |
| `text` | `contains`, `not-contains`, `starts-with`, `eq`, `ne`, `is-empty`, `is-not-empty` |
| `integer`, `real` | `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `between`, `is-empty`, `is-not-empty` |
| `boolean` | `is-true`, `is-false`, `is-empty` |
| `date` | `eq`, `ne`, `before`, `after`, `between`, `is-empty`, `is-not-empty` |

Notes on operators and values:

- **Enums:** they compare only by equality. There is no ordering, so `lt`, `ge` and the like are invalid on an enum attribute.
- **Undeclared attributes:** an attribute that isn't in the schema is treated as `text`.
- **When `op` is checked:** the loader rejects only an unknown operator code. An operator that doesn't fit the attribute's kind is caught when the filter is evaluated.
- **`between`:** the `value` is `<lo>|<hi>`, inclusive.
- **Value format:** dates and numbers use the same canonical strings as object attributes.
- **Operators without a value:** `is-empty`, `is-not-empty`, `is-true`, `is-false`, `present`, and `absent`.

The library exposes the filter engine as `mellon.evaluate_filter(doc, filter) -> set[str]`. Cross-document `has-incoming` rules take an optional `incoming_provider` callable. The GUI builds one from `DocumentService.incoming_links`, so a rule like "has-incoming from VCU3-STS: present" works across documents.

**Active-filter persistence (GUI).** The GUI remembers the slug of the active filter per document, under the QSettings key `grid/active_filter/<doc_id>`.

- **Where it is stored:** on Windows, in the registry at `HKCU\Software\mellon\mellon\grid\active_filter\<doc_id>`. On Linux and macOS, in the standard `QSettings("mellon","mellon")` location.
- **Reopening:** when you reopen the document, the matching filter from `<doc>/filters/` is applied again.
- **Clearing:** the pill's `[×]` button or the `(none)` menu entry removes the key. The filter file itself is kept.

## Surfaces

### GUI: `mellon.exe` (installer) or `mellon-gui [path]` (dev)

The GUI has two kinds of window:

- **Project Explorer:** lists the repo's documents. Double-click a document to open it in its own **Document Editor** window.
- **Document Editor:** three panes.
  - **Left:** the outline, with its own edit toolbar.
  - **Center:** the editable objects grid, with a suspect-state gutter.
  - **Right:** the Inspector and History tabs.

Inline edits write to disk immediately and are committed through the commit dialog. `mellon-gui --version` prints the version. The windowed `mellon.exe` has no console, so check an installed copy with `mellon-mcp.exe --version`.

**Keyboard shortcuts in the Project Explorer:**

| Keys | Action |
|---|---|
| Ctrl+N | New document |
| Ctrl+O | Open repository |
| F5 | Refresh: re-read every document and re-list documents that fail to load |

Quit is in the hamburger menu. It uses the platform's standard Quit shortcut, which Windows does not have.

**Keyboard shortcuts in the Document Editor:**

| Keys | Action |
|---|---|
| Ctrl+N / Ctrl+Shift+N | New sibling / new child object |
| Tab / Shift+Tab (grid, not editing), Ctrl+] / Ctrl+[ | Indent / outdent |
| Alt+Up / Alt+Down | Move object up / down |
| Del | Delete object (always asks first) |
| Ctrl+L | Add outgoing link |
| Ctrl+T | Trace view (up and down) |
| Ctrl+G | Attach or replace the selected object's figure |
| Ctrl+S or Ctrl+K | Commit (opens the commit dialog) |
| Ctrl+E | Export to PDF |
| Ctrl+F | Focus the quick filter box (id or text) |
| Ctrl+Shift+F | Advanced filter dialog (saved filters) |
| F5 | Reload from disk |
| Ctrl+Z / Ctrl+Y or Ctrl+Shift+Z | Undo / redo the last edit in this window (in a text field: its own text) |
| Ctrl+W or Ctrl+F4 | Close the window |
| Ctrl+Enter / Esc | In a description or table-cell editor: save / cancel. Plain Enter inserts a newline. |

New, Child, Indent/Outdent (Ctrl+] / Ctrl+[), Move up/down and Del sit on the outline pane's toolbar; their shortcuts also work while that pane is hidden. Tab / Shift+Tab in the grid indent and outdent too.

Acknowledging suspect links has no shortcut; see [the suspect-link workflow](#the-suspect-link-workflow). Validate, Properties, the Figure/Table/Views/Filters menus, Discard, and Read-only have no shortcuts either. They are on the toolbar or in the hamburger menu.

**Figures in draw.io.** The **Figure** toolbar menu and the row's right-click menu start with **New draw.io figure…** (the object has no figure yet: attaches a blank draw.io figure and opens it) or **Edit in draw.io…** (opens the existing one). Draw, save in draw.io (Ctrl+S), and switch back: the thumbnail refreshes and the object counts as changed, with no "Changed on disk" banner. The action is disabled for a plain SVG, which draw.io can't edit, and needs draw.io desktop; see **PDF export** below for where Mellon looks for it. Thumbnails and **View full size** draw figures on white and show draw.io's labels and colours.

**Banners above the grid:**

| Banner | Shown when | Controls |
|---|---|---|
| Suspect (amber) | The document has suspect or broken outgoing links. | **Suspects only** narrows the grid to the affected rows. **Ack selected** acknowledges every suspect link on the selected object. |
| Changed on disk (amber) | When the window is activated, Mellon finds that this document or one of its parents was changed on disk by another window, the MCP server, or a git pull. A figure of this document saved in place (by draw.io) is the exception: its thumbnail just refreshes. | **Reload** (F5). Until you reload, every edit is refused with "Reload first (F5)" so that cached text can't overwrite the change. Text you typed but didn't save is not written. |
| Read-only (blue) | Read-only mode is on (hamburger menu → View → Read-only). | **Enable editing**. |

**Delete.** Del always asks for confirmation. Delete is refused in these cases:

- The object has sub-objects in the outline. Move or delete them first.
- Any document in the repo fails to load, because the links to the object couldn't be checked.

If other objects link to the deleted object, a second confirmation lists those links. Mellon then strips them from the other documents' `links/` files, and those documents need their own commit. Deleting an object also removes its outgoing links file and its figure.

**PDF export (Ctrl+E).** Export renders the document as it is on disk, including uncommitted edits.

- **Cover and header:** they come from the manifest metadata. The revision label is the current issue.
- **Attributes shown:** `type`, `status`, `priority`, and `verification-method`, when the object has them.
- **Background job:** the export runs in the background, so the window stays usable.
- **Figures:** draw.io desktop renders every attached figure to a cropped vector PDF in one run, and each prints as a numbered figure, scaled down to fit when it is large:
  - On an ordinary object, the figure replaces the requirement text inside the card and the description becomes its caption: **Figure N:** *description*. The attributes still print below.
  - On a heading or a table object, the figure follows the heading or the table, captioned **Figure N** only, since the description already prints there.
  - A plain SVG (not made with draw.io) renders too.
  - A figure draw.io can't render (an empty diagram) or every figure when draw.io isn't installed prints a placeholder line instead, and a dialog lists the affected objects.
- **Traceability:** a document that declares parent documents (Properties, Parents tab) ends with a numbered **Traceability** section on a new page. MCP `export_document` produces the same PDF.
  - A summary: how many objects link to a parent, the count of links in each state, and per parent how many of its objects are covered.
  - **Links to** *parents*: a link table with one row per object in outline order, headings excluded: the object id and each parent object it links to. An object without such a link shows *none*. The column heads repeat on every page.
- **Suspect links are checked first.** The PDF shows no link states, so before the save dialog Export checks the document's outgoing links. If any are suspect or broken, a warning lists them (the first 10) with **Export anyway** and **Cancel** (the default). MCP `export_document` exports regardless and returns them in `suspect_links`.
  - **Uncovered objects in** *parent*, per parent: the ids of its objects (headings excluded) that no object in this document links to. A parent that fails to load is named instead.

Figures need draw.io desktop (<https://www.drawio.com/>). Mellon looks for it in this order: `MELLON_DRAWIO`, then the standard install folders (`%ProgramFiles%\draw.io\` and `%LOCALAPPDATA%\Programs\draw.io\`), then `PATH` (`drawio` or `draw.io`).

Export needs the `tectonic` binary. Mellon looks for it in this order:

1. `MELLON_TECTONIC`.
2. `<exe-dir>\tools\` in the installed build, which ships tectonic and a pre-filled TeX cache so that export works offline.
3. `PATH`.
4. `<project-root>/tools/` in a source checkout.

**Error log.** Unexpected errors are shown in a dialog and appended to `%LOCALAPPDATA%\mellon\mellon\logs\mellon-gui.log`. That is Qt's per-user app-data folder; on other operating systems the log is under the equivalent folder. If that folder can't be determined, the log goes to `<temp>\logs\mellon-gui.log`.

### MCP: `mellon-mcp [--repo-root <path>] [--read-only]`

`mellon-mcp` is a FastMCP stdio server with **45 tools**. In the installer it is `mellon-mcp.exe`.

**Options:**

| Option | Behaviour |
|---|---|
| `--repo-root` | Optional. Defaults to `$CLAUDE_PROJECT_DIR`, which Claude Code sets, and then to the current directory. The root must meet the [repository requirements](#repository-requirements), or the server logs why and exits with status 2. A root with no documents starts with a warning. |
| `--read-only` | Every write tool, plus `commit_changes`, `tag_issue`, and `export_document`, refuses with a `PermissionError`. |
| `--log-level` | `DEBUG`, `INFO` (the default), `WARNING`, or `ERROR`. Logging goes to stderr. |
| `--version` | Prints the version. |

For client configuration (`claude mcp add`, Claude Desktop), see [INSTALL.md](INSTALL.md).

| Group | Tools |
|---|---|
| Read (13) | `list_documents`, `show_document`, `search_objects`, `show_object`, `inspect_object`, `outline`, `validate_document`, `validate_repo`, `repo_health`, `suspects`, `trace`, `incoming_links`, `coverage_report` |
| Write (27) | `new_object`, `set_attribute`, `edit_description`, `update_object`, `copy_object`, `bulk_set_attribute`, `set_figure`, `clear_figure`, `set_table`, `clear_table`, `edit_metadata`, `tag_issue`, `add_parent_doc`, `remove_parent_doc`, `add_attr_option`, `define_attribute`, `change_attribute_required`, `delete_attribute`, `remove_attr_option`, `manage_view`, `create_document`, `move_object`, `reorder_outline`, `add_link`, `remove_link`, `delete_object`, `ack_suspects` |
| Git, review, export (5) | `commit_changes`, `git_history`, `show_object_at`, `review_suspect_link`, `export_document` |

Composite tools that save AI clients many calls:

- **Authoring:**
  - `new_object(doc_id, attrs=…, links=…)` creates an object with its attributes and links in one call.
  - `update_object(doc_id, obj_id, description=…, attrs=…, add_links=…, remove_links=…)` replaces what used to be 5–10 per-field calls.
  - `copy_object(doc_id, obj_id, count=N)` duplicates an existing requirement, for template-style authoring.
- **Bulk:**
  - `bulk_set_attribute(doc_id, obj_ids, name, value)` handles "approve these 30 requirements".
  - `reorder_outline(doc_id, ordering)` restructures the outline declaratively.
- **Schema:** `define_attribute(doc_id, name, kind, …, default_value="...")` adds an attribute and fills every existing object with the default.
- **Discovery:**
  - `search_objects(query, …)` does a case-insensitive substring search, with an optional attribute filter.
  - `inspect_object(doc_id, obj_id)` returns the object together with its trace and history.
- **Repo-wide:** `repo_health()` and `validate_repo()` answer "what state is this in?" in one call.
- **Compact reads:** `list_documents` and `show_document` return counts and `current_issue` by default. Pass `include=["metadata", "parents", "attributes", "outline", "views", "filters"]` (or `["*"]`) for more.
- **Auto-commit:**
  - Every write tool except `tag_issue` takes an optional `commit_message=…`. When it is set, the files the call changed are committed with a `Via: mellon-mcp` trailer, and the response carries `{commit: {short_hash, commit_message}}`.
  - If that commit fails, the response carries `commit_error` instead, and the write itself stays on disk.
  - Without `commit_message`, commits stay explicit through `commit_changes`.
- **Paths:**
  - `set_figure` reads `source_path`, and a relative path resolves against the repo root.
  - `export_document` writes `out_path`. That path must end in `.pdf` and must not be inside `.git/`. Figures are rendered through draw.io desktop, as in the GUI. The tool returns `unrendered_figures` when figures were left out, plus `drawio_found: false` when draw.io isn't installed. A document with parents ends with the Traceability section, as in the GUI. The document's suspect and broken outgoing links come back in `suspect_links` (same entries as `suspects`); they don't stop the export.

Naming notes:

- `set_attribute` sets an object's value. `define_attribute` adds a schema entry.
- The four old view tools are merged into `manage_view(action=…)`.

### Library

Every operation is also available from Python. The `mellon` package exports the following (see `src/mellon/__init__.py`):

| Area | Names |
|---|---|
| Load | `load_document(folder)`, `load_all(repo_root, load_errors=…)`, `discover_documents`, `LoadedDoc`, `DocLoadError`, `LoaderError` |
| Model | `Document`, `Metadata`, `Attribute`, `OutlineNode`, `RequirementObject`, `Link`, `Table`/`TableRow`/`TableColumn`/`TableCell`, `View`, `Filter`/`FilterRule`, `table_geometry_errors` |
| Validate | `validate(doc) -> list[ValidationError]` |
| Edit (writes files, never commits) | `create_object`, `set_attribute`, `set_description`, `set_table`/`clear_table`, `set_figure`/`clear_figure`, `add_link`/`remove_link`, `move_object`, `set_outline`, `delete_object`, `set_metadata`, `add_parent`/`remove_parent`, `add_attribute`, `add_attr_option`/`remove_attr_option`, `change_attribute_required`, `delete_attribute`, view and filter functions, `EditorError` |
| Suspects | `scan_document(doc, folder)`, `scan_repo(repo_root)`, `ack_all(folder, statuses)`, `LinkState`, `LinkStatus` |
| Traverse, search, filter | `trace_up`/`trace_down(docs, doc_id, obj_id)`, `repo_incoming_links`, `search_objects(docs, query, …)`, `evaluate_filter`, `valid_ops_for_kind` |
| Issues | `increment_issue`, `validate_issue` |
| Diff (library only, no GUI or MCP) | `compare_document(repo_root, doc_id, ref_a, ref_b) -> DocumentDiff` |

```python
from pathlib import Path
from mellon import load_document, validate, scan_document, LinkState

folder = Path("my-project/VCU3-SRS")
doc = load_document(folder)
errors = validate(doc)
suspects = [s for s in scan_document(doc, folder) if s.state is LinkState.SUSPECT]
```

`mellon.service.DocumentService` adds what the GUI and MCP layer on top of the library:

- **Caching:** an optional repo cache.
- **Read-only mode:** an optional read-only switch.
- **Errors:** every failure raises `ServiceError` with an `ErrorCode`.
- **Results:** writes return `MutationResult` objects.
- **Commits:** `commit`, `commit_document`, and `tag_issue`.
- **Recovery:** `discard_document_changes`.

```python
from mellon.service import DocumentService

svc = DocumentService("my-project")
report = svc.coverage_report("VCU3-SRS", "VCU3-STS", direction="incoming")
failed = []
docs = svc.get_all_documents(load_errors=failed)  # failed: list[DocLoadError]
```

## Committing

### Committing from the GUI

Commit (Ctrl+S or Ctrl+K) works on the current document only.

1. The document is validated first, and **any validation error blocks the commit.** Commit is also refused while the "Changed on disk" banner is up or the window is read-only.
2. The commit dialog opens:
   - The **message** is prefilled as "Edited <doc>. Affected requirements: …".
   - The **files** list shows every uncommitted change inside the document's folder, including changes made by the MCP server or by hand.
   - Warnings list parent documents that also have uncommitted changes, and this document's suspect or broken links. These warnings are informational only.
3. The **issue**, **author**, and **date** fields are explained under [Issue tags](#issue-tags). Author and date are written to the manifest and land in the same commit.
4. The optional **Tag** section tags the new commit with a name of your choice, such as `CDR-1.0`. See [Tags](#tags).

### What gets committed

| Path | Commits |
|---|---|
| GUI commit dialog | Only the dirty files inside the current document's folder. |
| MCP `commit_changes()` with no `paths` | Every uncommitted change inside the document folders, including folders deleted or renamed since the last commit. Nothing else in the work tree is included. |
| MCP `commit_changes(paths=[…])` or `commit_message=` on a write tool | Exactly those paths. |

In every case the commit is HEAD plus the selected paths, each with its work-tree content. A file someone staged in another git client counts as an uncommitted change of its folder; staged files outside the selection stay out of the commit, and they stay staged.

### When a commit is refused

| Situation | Message points to |
|---|---|
| `user.name` or `user.email` not set | The `git config` commands to run |
| A merge, rebase, or similar operation is in progress, or there are unresolved conflicts | "finish the merge in your git client" |
| Another git program holds `index.lock`, or commits at the same moment | "commit again" (nothing was committed) |
| Nothing to commit | — |

## Issue tags

A document's issue is a lightweight git tag named `<doc-id>-issue-<n>`, for example `VCU3-SRS-issue-2` or `VCU3-SRS-issue-12.23.55`. It is never stored in the manifest.

- **Format:** dot-separated numbers. Comparison is per component, so `1.10` is higher than `1.9`.
- **Current issue:** the highest `<doc-id>-issue-*` tag reachable from HEAD. Tags that exist only on other branches don't count.
- **Where it shows:** in the commit dialog, in the `current_issue` field of MCP `list_documents` / `show_document`, and as the revision on the PDF cover.

**Commit dialog fields:**

| Field | Behaviour |
|---|---|
| Issue | Prefilled with the **current** issue, or empty for a document that has none yet. Leave it as it is for a plain commit, which creates no tag. Raise it (for example 2 → 3) to tag the new commit `<doc-id>-issue-3`. A new issue must be higher than the current one. A raised issue alone is not a change: there must also be modified files or an author/date edit. |
| Author | Prefilled from the manifest, or from git `user.name` when the manifest has none. It must not be empty. |
| Date | Prefilled with the manifest's own date. It is **not** set to today automatically, because that would make every commit touch the same line. You can change it (`YYYY-MM-DD`) but not clear it once it is set. |

If creating the tag fails, the status bar shows "Tag failed: …" and the commit stays.

**From MCP:** `tag_issue(doc_id, issue)` tags HEAD. Commit first, then tag. The same rules apply: the issue must be dot-numeric and higher than the current one, and an existing tag is never overwritten.

**From git:**

```
git describe --tags --abbrev=0 --match "VCU3-SRS-issue-*"   # nearest issue tag (needs --tags: issue tags are lightweight)
git tag --list "VCU3-SRS-issue-*" --sort=-v:refname         # every issue of the document, highest first
git push --tags                                              # publish new issue tags
```

A plain `git push` doesn't push tags, and `git push --follow-tags` pushes only annotated tags. After cutting an issue, run `git push --tags`.

## Tags

A tag names a commit of the whole repository, for example `CDR-1.0` at a design review. It marks every document as it was last committed at that point, so it is the snapshot to verify against or to come back to.

- **Creating one:** fill in **Name** (and optionally **Message**) in the commit dialog's **Tag** section. The new commit is tagged. Leave the name empty for a plain commit.
- **With nothing to commit**, for example at a milestone when everything is committed, Commit still opens the dialog. Enter a name and the button turns into **Tag**: it tags the current commit and makes no new one. A raised issue needs a change to commit, so it can't be cut this way.
- **The tag is annotated.** It records who tagged and when, and the message, which defaults to the name.
- **Names:**
  - git's rules apply: no spaces, none of `~ ^ : ? * [ \`, no `..`, and no leading `-`;
  - a name that already exists is refused;
  - so is the issue-tag form `<doc-id>-issue-<n>`; raise the issue for that instead.

  The dialog checks the name as you type.
- **Uncommitted changes elsewhere stay out of it.** The commit holds only this document, and the tag marks the other documents as last committed. While a name is entered, the dialog lists the open points of the documents this one links up to, all the way up the link chain:
  - uncommitted changes;
  - suspect or broken links;
  - validation errors;
  - a document that fails to load.

  These are warnings and don't block the commit. Documents beside or below this one aren't checked.
- **Issue and tag together:** raising the issue and naming a tag in the same commit creates both tags on it.
- If creating the tag fails (for example, someone created the same name meanwhile), the status bar shows "Tag failed: …" and the commit stays.

**From git:**

```
git tag -n --list                  # every tag with its message
git show CDR-1.0 --stat            # who tagged it, when, and the commit
git push --tags                    # publish it (git push --follow-tags also works: the tag is annotated)
```

## The suspect-link workflow

This is the core quality gate. Assume `VCU3-SRS-1` satisfies `VCU3-SyRS-42`, and the link carries the correct `ack`.

1. **Revise the parent.** Edit `VCU3-SyRS/objects/VCU3-SyRS-42.xml` or its figure `VCU3-SyRS/figures/VCU3-SyRS-42.svg`, then commit. Both files count: the target's *current hash* is the last commit that changed either one.
2. **Scan.** The link on `VCU3-SRS-1` is now SUSPECT, because its stored `ack` no longer matches the parent's current hash.
   - The GUI shows the amber gutter and the suspect banner. It re-reads link states when you return to a window after HEAD has moved.
   - MCP `suspects` reports the link.
3. **Review** what changed on the parent:
   - **GUI:** double-click the outgoing link in the Inspector to jump to the parent, then use the History tab ("View at this commit…").
   - **MCP:** `review_suspect_link` returns the parent text at the ack and now, plus the commits in between.
   - **git:** `git log -p -- VCU3-SyRS/objects/VCU3-SyRS-42.xml VCU3-SyRS/figures/VCU3-SyRS-42.svg`.
4. **Update the child if needed.** Edit `VCU3-SRS-1` and commit.
5. **Acknowledge.** This rewrites the `ack` in `VCU3-SRS/links/VCU3-SRS-1.xml` to the parent's current hash. You can do it in any of these ways:
   - **Inspector:** the **Ack** button on that outgoing link acks one link.
   - **Suspect banner:** **Ack selected** acks every suspect link on the selected object.
   - **MCP:** `ack_suspects(doc_id)` acks every suspect link in the document. Pass `link_keys=[{source_id, target_doc, target_id}, …]` for a subset, or `dry_run=True` to preview.
   - **Library:** `ack_all(folder, statuses)`.

   The GUI has no keyboard shortcut and no whole-document ack. Commit the links file after acking.
6. **Re-scan.** The suspects are clean.

Broken links can't be acked. A link is BROKEN when its target file is missing, or when the target is not in git history.

The two-commit structure is enforced by git: a commit can't contain its own hash. If you revise a parent and update its ack in the same commit, the `ack` still points at the old hash, and the scan flags it. That forces a second, explicit commit that records the review.

### Merges, squashes and rebases

The current hash follows git's history simplification. It is the same commit that `git log -1 --format=%h -- <object.xml> <figure.svg>` reports from HEAD.

- **Merge commits keep acks valid.** Branch commits keep their hashes through a merge, whether fast-forward or `--no-ff`. A merge commit counts as a change only when its result differs from every parent, for example a conflict resolution that produced new text. Links to that object then turn SUSPECT, which is correct.
- **Squash merges, rebases, and cherry-picks invalidate acks.** They create new commits with new hashes, so acks recorded on the branch no longer match, and those links read SUSPECT after the merge. Re-ack on the target branch. For this reason the team routine is merge commits only (see [TEAM.md](TEAM.md)).
- **Upgrading from 0.1.** Mellon 0.1 reported a merge commit as the target's hash when the merge brought in a parent change. Links acked against such a merge commit read SUSPECT once in 0.2.0. Re-ack them once.

## Documents that fail to load

A document whose files can't be parsed is listed with its error instead of disappearing. Typical causes are a file left with merge-conflict markers after a pull, or a hand edit that broke the XML. The error names the file and line, and adds "(unresolved merge conflict?)" when it finds conflict markers.

| Where | What you see |
|---|---|
| Project Explorer | The document is greyed out with a warning icon, and its tooltip shows the error. Double-clicking it shows the error instead of opening the document. |
| Open editor, F5 | The real load error. The window keeps the last good copy behind the "Changed on disk" banner, and edits are refused. |
| MCP | `list_documents` lists it as `{doc_id, folder, load_error: {doc_id, file, line, message}}`. `validate_repo` returns `ok: false` with `load_errors`. `repo_health` adds `load_errors` (the other fields are then incomplete). |
| Delete | Refused repo-wide until every document loads again. |

To recover, fix the named file (for merge conflicts, see [Merging branches](#merging-branches)), then press **F5** in the Project Explorer and in open editors, and run **Validate**. After any pull, press F5: the Explorer notices added or removed document folders by itself, but not a document that broke in place.

## Merging branches

Work can happen on several branches at once. New objects get the next free number on their branch, so two branches often create the same id. When git merges them (a pull, or merging a branch in your git client), it conflicts on those files, and it merges others as text in ways that are wrong for documents: a duplicate outline entry, or a link one branch made to its new object ending up on the other branch's object. Mellon resolves document merges itself.

While a merge is in progress, the Project Explorer shows **Merge in progress** (checked on F5 and when the window is activated). **Resolve merge…** reads the three commits involved (the common ancestor, yours and theirs), not git's conflict markers, and works like git does, per object:

- **Same id created on both branches: renumbered, no question.** It isn't a conflict. The requirement committed later yields and becomes the next id free on both branches. Its object, links and figure files, its place in the outline, and every link its branch made to it (in any document) follow it. The dialog shows one line per renumbered requirement.
- **Changed on one branch only: taken.** Not shown.
- **Same requirement changed on both branches: merged per field.** Different fields (say the status on one branch and the text on the other) merge by themselves, like git merges separate lines.
- **Same field changed on both branches: merged by hand.** The block for the requirement starts with **Traces to**: the requirements it links to, with their text, so you can judge the merge against what it traces to. Then, per field:
  - **Text:** both versions, and an editable result prefilled with yours. Write the merged text, or use **Use mine** / **Use theirs**.
  - **Attribute value:** a dropdown with your value (the default) and theirs.
  - **Table or figure:** keep yours (the default) or theirs.
  - What came in from their branch without a conflict is listed as "Also merged in".
- **Deleted on one branch, changed on the other:** **Keep** the requirement (ticked by default) or untick to delete it.
- **Document properties:** merged per field too. Metadata fields both changed are merged by hand like text; parents are united; an attribute definition changed on both sides is kept from one side (yours by default).
- **Views, filters and rules changed on both branches:** keep yours (the default) or theirs.
- **Merged automatically.** Link files merge as sets: both sides' new links are kept, and a link changed on both sides keeps yours. The outline merges by object position: their additions, moves and deletions are applied to yours, and an object moved on both sides stays where you put it.
- **Outside documents.** Conflicts in other files are left for your git client.

Every choice has a default, so **Resolve** works straight away. It writes the result and marks the files resolved in the index. Make the merge commit in your git client, then F5 and **Validate**. It can be run again: it starts from the commits each time.

- **After the merge:** links to a renumbered requirement read SUSPECT (its file is new in the merge commit); review and ack them.
- **Library:**
  - `mellon.merge.plan(root)` returns the renumbered requirements and the hand merges, with each requirement's links.
  - `mellon.merge.resolve(root, edits=…, delete=…)` writes the result: merged texts, or `"ours"` / `"theirs"`, per field.
  - `mellon.merge.pending(root)` lists the conflicted paths of a merge in progress.
  - MCP has no merge tools.

## Undo and redo

**Ctrl+Z** in a Document Editor undoes the window's edits one at a time, newest first; **Ctrl+Y** (or Ctrl+Shift+Z) redoes. Both are also under **Menu → Edit**, named after the edit ("Undo set attribute").

- **What a step is:** one edit as the window made it, with every file it changed. Deleting an object is one step, including the links it stripped from other documents; undo brings them all back.
- **Text being typed:** a focused text field keeps Ctrl+Z for its own text. Elsewhere, pending typed text is saved first, so Ctrl+Z after typing undoes the typing.
- **How far back:** to the last commit. The history is cleared when you commit, press F5 or Discard, when the "Changed on disk" banner comes up, and when a commit from a git client leaves the document with nothing uncommitted.
- **Not covered:** figure edits saved from draw.io (draw.io has its own undo), and anything done through MCP.
- **Refused:** if a file of the step changed since (MCP, another window, draw.io), undo would overwrite that change, so it says so instead and clears the history.
- **Read-only:** undo and redo are disabled.

Every editor operation is also all or nothing on disk: if one of its files can't be written, the files it already changed are put back.

## Discarding uncommitted changes

The Document Editor's hamburger menu has **Discard uncommitted changes to this document…**. It puts the whole document back to its last commit in one go.

- **What it does:** puts the document's folder back to its last commit.
  - Edited objects, links, attributes, metadata, views, and filters are restored.
  - Files added since the last commit (new objects, links, figures, views, and filters) are deleted.
  - Staged changes in the folder are unstaged.
- **Scope:** other documents are not touched. Their links to objects deleted by the discard become BROKEN.
- **Confirmation:** it always asks first, and it cannot be undone.
- **When it is available:** in a writable window, when the document has uncommitted changes and has been committed at least once. It also works while the "Changed on disk" banner is up.
- **When it is refused:**
  - A merge is in progress.
  - Another git program holds `index.lock`.
  - The folder contains something Mellon won't restore: a nested git repository, or a symlink or submodule in the last commit.
  - If it fails partway, run it again to finish.
- **MCP and library:** MCP has no discard tool. The library call is `DocumentService.discard_document_changes(doc_id)`, which returns the paths it changed.

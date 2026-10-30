# Mellon

Mellon is a requirements management tool modelled on IBM DOORS, built for small engineering teams. Each requirements document is a folder of XML files in a git repository: a manifest (metadata, attribute schema, outline), one file per object, and a links file for each object that has outgoing links. Git provides versioning, branching, history, diffs and review, and one-file-per-object keeps diffs and merges at the level of a single requirement. Mellon adds the document editor, links with suspect-link review, validation, traceability and PDF export. It has a desktop GUI for engineers and an MCP server so AI agents can work on the same requirements under the same rules.

The current version is 0.3.0. Mellon is sized for documents of up to about 500 objects.

## Features

- **Document editor.** Outline tree on the left, an editable attribute grid in the middle, and an Inspector and History tab on the right. Each document opens in its own window from the Project Explorer.
- **Schema and validation.** Each document declares its own attributes (enum, text, integer, real, boolean, date; optional or required) and can add conditional rules in `<doc-id>.rules.xml`. The GUI refuses to commit a document that has validation errors. MCP commits don't check, so agents should run `validate_document` first.
- **Links and suspect-link review.** An object can link to objects in its own document or in a declared parent document. When the target changes in a later commit, the link turns SUSPECT. It stays SUSPECT until someone reviews it and acknowledges it ("ack"), and the ack is committed.
- **Trace and coverage.** The up/down trace view is in the GUI (`Ctrl+T`). Coverage between a document and its parent or child is reported over MCP (`coverage_report`, `repo_health`) and in the library.
- **Saved views and filters.** A view saves column visibility and order; a filter is a DOORS-style multi-rule filter. Both are stored as files in the document folder, so they are versioned with it.
- **Tables and figures.** A table belongs to one object; it can have header rows, column alignment and merged cells, and is edited in place in the grid. Each object can also have one SVG figure (draw.io `.drawio.svg` works), shown as a thumbnail in the grid.
- **Issue tags.** You cut a document issue as a git tag `<doc-id>-issue-<n>` from the commit dialog. A plain commit creates no tag.
- **PDF export** (`Ctrl+E`). The cover and running header come from the document's ID, title, author, date and current issue. Tables are typeset. Attached figures are rendered by draw.io desktop; without it, each one prints as a placeholder line. The Windows installer includes the TeX engine (tectonic) and its cache, so export works offline.
- **MCP server for AI agents.** `mellon-mcp` has 45 tools (13 read, 27 write, 5 git/review). Use `--read-only` to refuse every write.
- **Safe with git.**
  - A GUI commit contains only that document's files, never unrelated staged changes.
  - Commits are refused without a git identity or while a merge is in progress.
  - Documents that fail to load, for example with unresolved conflict markers, are listed with their file and line instead of disappearing.
  - A "Changed on disk" banner blocks edits until you reload (`F5`).
  - **Discard uncommitted changes to this document…** in the editor menu puts a document back to its last commit.

## Install

**Windows.** Download `Mellon-Setup-<version>.exe` from the [Releases page](https://github.com/tobias-hallberg/mellon-req-tool/releases) and run it, or build it yourself (see [Building the installer](mellon/INSTALL.md#building-the-installer)).

- It installs per user to `%LOCALAPPDATA%\Programs\Mellon` without admin rights and adds a **Mellon** Start-menu entry.
- You also need Git for Windows and a git client for pull and push.
- The executables are unsigned. If the installer was downloaded from the web, SmartScreen may show **More info → Run anyway**.
- Close Mellon and Claude Code before you upgrade.

Details, including MCP client setup, are in [mellon/INSTALL.md](mellon/INSTALL.md).

**Developers (any OS)** use [uv](https://docs.astral.sh/uv/). The Python package is one folder down, in `mellon/`:

```
git clone https://github.com/tobias-hallberg/mellon-req-tool.git
cd mellon-req-tool/mellon
uv sync
uv run mellon-gui [path/to/requirements-repo]
uv run mellon-mcp --repo-root path/to/requirements-repo
uv run pytest
```

- To put `mellon-gui` and `mellon-mcp` on your PATH, run `uv tool install ./mellon` from the outer folder.
- PDF export outside the installer needs `tectonic` on PATH, or `MELLON_TECTONIC` set to its path.
- An environment created for 0.1 (which depended on the full `pyside6`) may break after `uv sync`. Repair it with `uv sync --reinstall-package pyside6-essentials --reinstall-package shiboken6`.

## Quickstart (5 minutes)

The example builds a parent document `DEMO-SyRS` and a child document `DEMO-SRS`. On Windows, use the Start-menu **Mellon**; developers use `uv run mellon-gui`.

Two things to know first:

- Editor windows open **read-only** and go back to read-only after each commit. Click **Enable editing** in the banner (or untick **Menu → Read-only**) before you edit.
- **Menu** is the button at the left end of the toolbar.

1. **Set your git identity once.** Mellon refuses to commit without it.
   ```
   git config --global user.name  "Your Name"
   git config --global user.email "you@example.com"
   ```
2. **Get a requirements repository.** For a scratch repo, run `git init C:\work\demo-reqs`. For a shared team repo, clone it to a local disk (not OneDrive or a network drive); see [mellon/TEAM.md](mellon/TEAM.md).
3. **Open it.** In Mellon, choose **Menu → Open Repository…** (`Ctrl+O`) and pick the folder that contains `.git`. Mellon refuses a subfolder of a clone, a folder that isn't in a git repository, and a shallow clone.
4. **Create a document.** In the Project Explorer, click **Document** (`Ctrl+N`) and enter the ID `DEMO-SyRS` and a title. An editor window opens.
5. **Add objects.**
   1. Click **Enable editing**, then **Add first object**. Use the **New sibling** and **New child** icon buttons under the outline (`Ctrl+N` / `Ctrl+Shift+N`) to add more objects or nest one. IDs are assigned for you: `DEMO-SyRS-0`, `DEMO-SyRS-1`, …
   2. Double-click a **Description** cell to type the text.
   3. Set **Type**, which is required. The default options are Heading, Information and Note. To add `Requirement`, open **Properties → Attributes**, select **Type** and click **Add option…**.
6. **Commit.** Click **Commit** (`Ctrl+S`), enter a message and leave **Issue** as it is for a plain commit. Raising it, for example to `1`, also tags the commit `DEMO-SyRS-issue-1`.
7. **Add a child document.**
   1. In the Project Explorer, create `DEMO-SRS`.
   2. In its window, click **Enable editing**, then **Properties → Parents → Add…** and choose `DEMO-SyRS`.
   3. Add an object and set its Type.
8. **Link.**
   1. Select the `DEMO-SRS` object, click **Link…** (`Ctrl+L`), choose `DEMO-SyRS / DEMO-SyRS-0` with link type `satisfies`, and click **OK**.
   2. Commit `DEMO-SRS`.

   The Inspector shows the link as `[OK]`, and the grid's state column shows a green marker. Link only to committed objects: a link to an object that was never committed shows as BROKEN.
9. **Revise the parent.** In the `DEMO-SyRS` window, click **Enable editing**, change the text of `DEMO-SyRS-0`, and commit.
10. **See SUSPECT.** Switch to the `DEMO-SRS` window and press `F5` if the amber **Changed on disk** banner shows.
    - The link is now `[SUSPECT]` and the grid marker turns yellow.
    - A banner reports "1 suspect link".
    - The **History** tab of `DEMO-SyRS-0`, in the parent's window, shows what changed.
11. **Ack and commit.** Click **Enable editing**, then **Ack** next to the link in the Inspector, or **Ack selected** in the banner. Commit `DEMO-SRS`. The link is `[OK]` again.

Revising a parent and acking its children always takes two commits. This is by design: a file can't record the hash of the commit it is part of. For daily team use (pull, `F5`, edit, commit, push), see [mellon/TEAM.md](mellon/TEAM.md).

## Using Mellon from Claude Code

Register the MCP server from inside the requirements repository (PowerShell; in cmd, use `%LOCALAPPDATA%`):

```
claude mcp add mellon -- "$env:LOCALAPPDATA\Programs\Mellon\mellon-mcp.exe"
```

- Without `--repo-root`, the server uses `CLAUDE_PROJECT_DIR`, then the current directory.
- Append `--read-only` to refuse every write tool.
- Like the GUI, the server refuses a root that isn't the top of a full git clone; it exits with status 2.
- Write tools change files only; they commit only when the agent passes `commit_message` or calls `commit_changes`.

[mellon/INSTALL.md](mellon/INSTALL.md) covers `claude mcp add`, Claude Desktop and uv installs.

## What Mellon expects of a repository

- The repository root is the git work-tree root, with full (not shallow) history.
- Each document is a folder directly under the root, named by its document ID and containing `<id>/<id>.xml`. Any top-level folder of that shape is treated as a document.
- LF line endings. Commit a `.gitattributes` with `* text=auto eol=lf` once per team repo.
- Pulling, pushing and merging happen in your git client. Mellon only commits.

## Troubleshooting

- **Unexpected errors** are shown in a dialog and logged to `%LOCALAPPDATA%\mellon\mellon\logs\mellon-gui.log`.
- **A document is greyed out** in the Project Explorer: it failed to load. The tooltip gives the file, line and error; this is often an unresolved merge conflict. Fix the file in your git client, then press `F5`.

## Documentation

| File | What it covers |
|---|---|
| [mellon/TEAM.md](mellon/TEAM.md) | Team routine: one-time setup, the daily loop, rules that prevent conflicts, the conflict playbook |
| [mellon/INSTALL.md](mellon/INSTALL.md) | Installer, MCP client configuration, developer setup with uv |
| [mellon/USAGE.md](mellon/USAGE.md) | On-disk format, attribute kinds, validation rules, filters, issue tags, the suspect-link workflow |
| [mellon/STATUS.md](mellon/STATUS.md) | What's built, what's deferred, source file map |
| [mellon/CHANGELOG.md](mellon/CHANGELOG.md) | Release notes, including behaviour changes in 0.2.0 |

## Limits in 0.3

- Mellon has no pull or push and doesn't start merges; use a git client. When a merge stops on conflicts in documents, **Resolve merge…** in the Project Explorer resolves them.
- Undo and redo cover the edits made in a Document Editor window since the last commit, not draw.io saves or MCP changes.
- Figures are SVG only, and rendering them in the PDF needs draw.io desktop.
- Descriptions are plain text.
- Mellon is sized for up to about 500 objects per document.

## License

GPL-3.0-or-later, © Tobias Hallberg. See [LICENSE](LICENSE). Licenses of the bundled third-party components are in [mellon/packaging/THIRD-PARTY-NOTICES.txt](mellon/packaging/THIRD-PARTY-NOTICES.txt).

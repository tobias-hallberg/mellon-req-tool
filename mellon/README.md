# Mellon

Mellon is a requirements management tool modelled on IBM DOORS. Requirements documents are stored as XML files in a git repository: a manifest per document, one file per object, and a links file for each object that has outgoing links. Git provides versioning, branching, history and diffs. Mellon adds:

- an outline, attribute grid and Inspector editor
- links with suspect-link review
- validation, trace and coverage
- saved views and filters
- tables and figures
- issue tags and PDF export

Version 0.2.0. License GPL-3.0-or-later, © Tobias Hallberg (see `LICENSE`).

## Entry points

| Command | What it does |
|---|---|
| `mellon-gui [PATH]` | PySide6 desktop app. A Project Explorer lists the repository's documents, and each document opens in its own editor window. |
| `mellon-mcp [--repo-root PATH] [--read-only] [--log-level LEVEL]` | MCP stdio server for AI agents (45 tools). `--repo-root` defaults to `$CLAUDE_PROJECT_DIR`, then the current directory; `--read-only` refuses every write tool. |

Both commands accept `--version`.

Both refuse a folder that is not the root of a full (non-shallow) git clone; `mellon-mcp` then exits with status 2. Documents are folders directly under that root. Mellon commits but never pulls or pushes.

## Install with uv

Requires Python 3.12 or later. [uv](https://docs.astral.sh/uv/) installs it if needed.

To put both commands on your PATH, run this from this folder (the one with `pyproject.toml`):

```
uv tool install .
```

After pulling new source, run `uv tool install --reinstall .`.

For development, also from this folder:

```
uv sync
uv run mellon-gui path/to/requirements-repo
uv run mellon-mcp --repo-root path/to/requirements-repo
uv run pytest
```

- **PDF export** needs the `tectonic` binary, either on `PATH` or with `MELLON_TECTONIC` set to its path.
- **Commits** need a git identity (`git config --global user.name` / `user.email`).
- **Upgrading from 0.1.** A 0.1 environment depended on the full `pyside6` and may break after `uv sync`. Repair it with `uv sync --reinstall-package pyside6-essentials --reinstall-package shiboken6`.

On Windows, you can use the `Mellon-Setup-<version>.exe` installer instead. It includes the GUI, the MCP server and tectonic; see `INSTALL.md`.

## Documentation

These files are in this folder:

- `INSTALL.md` ([INSTALL.md](INSTALL.md)): the installer, MCP client configuration, developer setup.
- `TEAM.md` ([TEAM.md](TEAM.md)): how a team shares one requirements repository.
- `USAGE.md` ([USAGE.md](USAGE.md)): the on-disk format, validation rules, filters, issue tags and the suspect-link workflow.
- `STATUS.md` ([STATUS.md](STATUS.md)): what's built and what's deferred.
- `CHANGELOG.md` ([CHANGELOG.md](CHANGELOG.md)): release notes.

"""Mutating operations on documents.

Everything in this module WRITES to disk. Callers commit the result themselves.

Note on formatting: we use `lxml.etree.indent()` to re-indent modified files to
a canonical 2-space style. This can collapse decorative blank lines that were
in the original — the first edit on a hand-authored file produces a one-time
formatting diff, after which edits are stable. For object and link files this
is a non-issue; for the manifest it may briefly surprise the user.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import os
import re
import shutil
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Optional, TypeVar

from lxml import etree

from .filter import (
    COMBINATORS,
    KINDS,
    LINK_OPS,
    valid_ops_for_kind,
)
from .git_ops import GitError, _replace_file, discover_repo, last_commit_for_paths
from .loader import LoaderError, load_document
from .repo import DocLoadError, load_sibling_docs
from .schema import (
    TABLE_ALIGNMENTS,
    Document,
    FilterRule,
    Table,
    table_geometry_errors,
)


class EditorError(Exception):
    """Raised when an editor operation can't proceed (bad inputs, conflicts)."""


_XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n'


# Heading is a built-in structural type (special-cased by the GUI, rule
# engine, coverage, and export). Information and Note are the default
# non-structural types. Documents that contain requirements add "Requirement"
# to their own type schema along with a RequirementType attribute.
DEFAULT_TYPE_OPTIONS = ("Heading", "Information", "Note")

DEFAULT_ATTR_SCHEMA: list[dict] = [
    {"name": "type", "kind": "enum", "required": True, "options": list(DEFAULT_TYPE_OPTIONS)},
    {"name": "rationale", "kind": "text", "required": False},
]


# Default rule set: Heading objects carry no body content.
DEFAULT_RULES: list[dict] = [
    {"when": {"type": "Heading"},
     "then": [("rationale", "forbidden")]},
]


_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_DELAY = 0.05  # seconds


def _write_tree(tree: etree._ElementTree, path: Path) -> None:
    """Serialize an lxml tree with a canonical header and trailing newline.

    lxml's `tree.write(xml_declaration=True)` emits single-quote attribute
    delimiters in the declaration and no trailing newline; both are harmless
    but produce needless git-diff noise against hand-authored files.

    The bytes (always LF) go to a temp sibling that `os.replace` then moves
    over `path`, so a failed write never leaves a truncated file behind.
    """
    _touch(path)
    body = etree.tostring(tree.getroot(), pretty_print=False, encoding="UTF-8")
    if not body.endswith(b"\n"):
        body += b"\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(_XML_HEADER.encode("utf-8"))
            f.write(body)
        # Windows refuses to replace a file another process has open without
        # FILE_SHARE_DELETE (a reader, antivirus, a sync client). Those holds
        # are brief, so retry a few times before giving up.
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_RETRY_DELAY)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _unlink(path: Path) -> None:
    """Delete a file the editor owns, recording it in the open journals."""
    _touch(path)
    path.unlink()


# --- write journal ----------------------------------------------------------
#
# Each file is written atomically on its own (`_write_tree`), but an
# operation that touches several files could still fail halfway. Every write
# and delete above records the file's previous bytes in each open journal
# first; `_atomic` runs every public mutation inside one that puts all those
# files back if the operation raises. The service records whole user actions
# the same way, and the GUI's undo replays the bytes.


@dataclass
class Journal:
    """Files an editor operation changed: their bytes before and, once the
    journal is sealed, after (None: the file doesn't exist)."""

    before: dict[Path, bytes | None] = field(default_factory=dict)
    after: dict[Path, bytes | None] = field(default_factory=dict)

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(self.after or self.before)

    def note(self, path: Path) -> None:
        path = Path(os.path.abspath(path))
        if path not in self.before:
            self.before[path] = _read_or_none(path)

    def seal(self) -> None:
        """Take each file's current bytes as its after state, dropping files
        that ended up as they were."""
        now = {p: _read_or_none(p) for p in self.before}
        self.before = {p: b for p, b in self.before.items() if now[p] != b}
        self.after = {p: now[p] for p in self.before}

    def restore(self) -> list[Path]:
        """Put every file back as it was before, best effort. Returns the
        files that couldn't be restored."""
        return _put_files(reversed(list(self.before.items())))

    def revert(self) -> None:
        """Undo a sealed journal: back to the before state.

        Raises `EditorError`, changing nothing, unless every file still has
        its after state: something else (another window, MCP, git, draw.io)
        changed it since, and undoing would clobber that.
        """
        self._switch(self.after, self.before, "undo")

    def reapply(self) -> None:
        """Redo a reverted journal: forward to the after state, with the same
        guard as `revert`."""
        self._switch(self.before, self.after, "redo")

    def _switch(
        self,
        expect: dict[Path, bytes | None],
        target: dict[Path, bytes | None],
        what: str,
    ) -> None:
        changed = [p for p, b in expect.items() if _read_or_none(p) != b]
        if changed:
            names = ", ".join(p.name for p in changed[:5])
            raise EditorError(
                f"can't {what}: changed on disk since ({names})"
            )
        failed = _put_files(target.items())
        if failed:
            names = ", ".join(p.name for p in failed[:5])
            raise EditorError(f"can't {what}: could not write {names}")


def _read_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _put_files(items) -> list[Path]:
    """Write each (path, bytes) atomically, deleting where bytes is None (and
    a `figures/` folder left empty, as the editor does). Returns the paths
    that failed; the rest are still written."""
    failed: list[Path] = []
    for path, data in items:
        try:
            if data is None:
                path.unlink(missing_ok=True)
                parent = path.parent
                if parent.name == "figures" and parent.is_dir():
                    with contextlib.suppress(OSError):
                        parent.rmdir()  # only succeeds when empty
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                _replace_file(path, data)
        except OSError:
            failed.append(path)
    return failed


_JOURNALS: contextvars.ContextVar[tuple[Journal, ...]] = contextvars.ContextVar(
    "mellon_editor_journals", default=()
)


def _touch(path: Path) -> None:
    """Record `path`'s current bytes in every open journal before it changes."""
    for j in _JOURNALS.get():
        j.note(path)


def journal_open() -> bool:
    """True inside a `journal()` block."""
    return bool(_JOURNALS.get())


@contextlib.contextmanager
def journal(*, rollback: bool = True) -> Iterator[Journal]:
    """Record every file the editor writes or deletes inside the block.

    With `rollback` (the default), an exception puts every recorded file back
    as it was and re-raises, so the block lands whole or not at all. Journals
    nest: a file written inside an inner one is recorded in the outer ones
    too. On a clean exit the journal is sealed (see `Journal.seal`).
    """
    j = Journal()
    token = _JOURNALS.set(_JOURNALS.get() + (j,))
    try:
        yield j
    except BaseException as e:
        if rollback:
            failed = j.restore()
            if failed:
                e.add_note(
                    "could not put back: "
                    + ", ".join(str(p) for p in failed)
                )
        raise
    finally:
        _JOURNALS.reset(token)
    j.seal()


_F = TypeVar("_F", bound=Callable)


def _atomic(fn: _F) -> _F:
    """Run an editor mutation inside a rollback journal: if it raises, every
    file it touched is put back."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with journal():
            return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


# XML 1.0 forbids every C0 control character except tab, LF and CR. Text
# pasted from Word or PDFs carries \x0b (manual line break) and \x0c (page
# break), so those become newlines; anything else is refused up front. CRLF
# and lone CR (Windows clients) become LF too, so no `&#13;` lands in diffs.
_XML_ILLEGAL_C0_RE = re.compile(r"[\x00-\x08\x0e-\x1f]")


def _sanitize_text(value: str, what: str) -> str:
    """Return user text safe to store in XML, or raise EditorError naming the bad character."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\x0b", "\n").replace("\x0c", "\n")
    m = _XML_ILLEGAL_C0_RE.search(value)
    if m is not None:
        raise EditorError(
            f"{what} contains control character U+{ord(m.group()):04X}, "
            "which XML cannot store"
        )
    return value


def _child_path(folder: Path, name: str) -> Path:
    """Return `folder / name`, refusing a name that would resolve outside `folder`."""
    path = folder / name
    # Resolve only the parent: a symlinked file inside `folder` is fine, a
    # name that steps into another directory ("../x", "sub/x", "..") is not.
    if name == ".." or path.parent.resolve() != folder.resolve():
        raise EditorError(f"invalid file name {name!r}: must stay inside {folder.name}/")
    return path


def allocate_next_id(doc: Document) -> str:
    """Return the next unused id for `doc`: `<doc.id>-<N>` with N = max+1 (or 0)."""
    prefix = f"{doc.id}-"
    used = []
    for obj_id in doc.objects:
        if not obj_id.startswith(prefix):
            continue
        tail = obj_id[len(prefix):]
        if tail.isdigit():
            used.append(int(tail))
    n = max(used) + 1 if used else 0
    return f"{prefix}{n}"


@_atomic
def create_object(
    folder: Path,
    parent_id: Optional[str] = None,
    description: str = "",
) -> str:
    """Create a new object in `folder`. Returns the new object id.

    If `parent_id` is given, nest the new object as its last child in the
    outline. Otherwise append as a new top-level sibling.
    """
    description = _sanitize_text(description, "description")
    doc = load_document(folder)
    new_id = allocate_next_id(doc)

    # The object file, then the outline ref (`_atomic` undoes both on failure).
    obj_path = folder / "objects" / f"{new_id}.xml"
    if obj_path.exists():
        raise EditorError(f"object file already exists: {obj_path}")
    obj_el = etree.Element("object", id=new_id)
    etree.SubElement(obj_el, "description").text = description
    tree = etree.ElementTree(obj_el)
    etree.indent(tree, space="  ")
    # git doesn't track an empty objects/, so a fresh clone may lack it.
    obj_path.parent.mkdir(parents=True, exist_ok=True)
    _write_tree(tree, obj_path)

    _add_outline_ref(folder / f"{doc.id}.xml", new_id, parent_id)
    return new_id


_DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def create_document(
    root: Path,
    doc_id: str,
    *,
    title: str = "",
    parents: list[str] | None = None,
    attr_schema: list[dict] | None = None,
) -> Path:
    """Create a new document folder + manifest under *root*. Returns the folder path.

    *attr_schema* entries: {"name": str, "kind": "enum"|"text", "required": bool,
    "options": [str]}  — options required for kind "enum".
    """
    if not _DOC_ID_RE.match(doc_id):
        raise EditorError(
            f"invalid doc_id {doc_id!r}: use only letters, digits, hyphens, underscores"
        )
    title = _sanitize_text(title, "title")
    folder = root / doc_id
    if folder.exists():
        raise EditorError(f"document already exists: {folder}")

    if attr_schema is None:
        effective_schema = DEFAULT_ATTR_SCHEMA
        write_default_rules = True
    else:
        effective_schema = attr_schema
        write_default_rules = False

    try:
        folder.mkdir()
        (folder / "objects").mkdir()
        _write_manifest(
            folder, doc_id, title=title, parents=parents or [], attr_schema=effective_schema
        )
        if write_default_rules:
            _write_rules_file(folder, doc_id, DEFAULT_RULES)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    return folder


def _write_manifest(
    folder: Path,
    doc_id: str,
    *,
    title: str,
    parents: list[str],
    attr_schema: list[dict],
) -> None:
    root_el = etree.Element("document", id=doc_id)
    meta = etree.SubElement(root_el, "metadata")
    etree.SubElement(meta, "title").text = title

    if parents:
        parents_el = etree.SubElement(root_el, "parents")
        for p in parents:
            etree.SubElement(parents_el, "parent", doc=p)

    attrs_el = etree.SubElement(root_el, "attributes")
    for a in attr_schema:
        name = a.get("name", "")
        kind = a.get("kind", "text")
        required = a.get("required", False)
        attr_el = etree.SubElement(
            attrs_el,
            "attribute",
            name=name,
            kind=kind,
            required="true" if required else "false",
        )
        if kind == "enum":
            for opt in a.get("options", []):
                etree.SubElement(attr_el, "option").text = opt

    etree.SubElement(root_el, "outline")

    tree = etree.ElementTree(root_el)
    etree.indent(tree, space="  ")
    _write_tree(tree, folder / f"{doc_id}.xml")


def _write_rules_file(folder: Path, doc_id: str, rules: list[dict]) -> None:
    """Write `<doc_id>.rules.xml` from a list of rule dicts.

    Each rule dict has shape:
        {"when": {predicate_name: value, ...},
         "then": [(attr_name, status), ...]}
    """
    root_el = etree.Element("rules")
    for rule in rules:
        rule_el = etree.SubElement(root_el, "rule")
        when_attrs = rule.get("when") or {}
        when_el = etree.SubElement(rule_el, "when")
        for k, v in when_attrs.items():
            when_el.set(k, v)
        then_el = etree.SubElement(rule_el, "then")
        for attr_name, status in rule.get("then") or []:
            etree.SubElement(then_el, "attr", name=attr_name, status=status)
    tree = etree.ElementTree(root_el)
    etree.indent(tree, space="  ")
    _write_tree(tree, folder / f"{doc_id}.rules.xml")


@_atomic
def set_attribute(folder: Path, obj_id: str, attr_name: str, value: str) -> None:
    """Set `attr_name = value` on an object, validated against the manifest schema.

    An empty value clears the attribute, enum ones included (as in the
    validator, where "" is never a bad option).
    """
    value = _sanitize_text(value, f"attribute {attr_name!r}")
    doc = load_document(folder)
    if obj_id not in doc.objects:
        raise EditorError(f"no object {obj_id!r} in document {doc.id!r}")

    attr = doc.attribute(attr_name)
    if attr is None:
        raise EditorError(
            f"attribute {attr_name!r} is not declared in the manifest schema"
        )
    if attr.kind == "enum" and value and value not in attr.options:
        raise EditorError(
            f"{value!r} is not a valid option for {attr_name!r} "
            f"(options: {list(attr.options)})"
        )

    obj_path = folder / "objects" / f"{obj_id}.xml"
    tree = etree.parse(str(obj_path))
    root = tree.getroot()

    existing = None
    for a in root.findall("attr"):
        if a.get("name") == attr_name:
            existing = a
            break

    if existing is not None:
        existing.text = value
    else:
        new = etree.Element("attr", name=attr_name)
        new.text = value
        # Insert before <description>/<figure> if present, else at end.
        description = root.find("description")
        if description is not None:
            description.addprevious(new)
        else:
            root.append(new)

    etree.indent(tree, space="  ")
    _write_tree(tree, obj_path)


@_atomic
def set_description(folder: Path, obj_id: str, text: str) -> None:
    """Replace the <description> text of an object. Multiline is fine."""
    text = _sanitize_text(text, "description")
    doc = load_document(folder)
    if obj_id not in doc.objects:
        raise EditorError(f"no object {obj_id!r} in document {doc.id!r}")

    obj_path = folder / "objects" / f"{obj_id}.xml"
    tree = etree.parse(str(obj_path))
    root = tree.getroot()

    description = root.find("description")
    if description is None:
        description = etree.SubElement(root, "description")
    description.text = text

    etree.indent(tree, space="  ")
    _write_tree(tree, obj_path)


def _validate_table(table: Table) -> None:
    """Raise EditorError unless `table` has valid alignments, spans, and tiling."""
    for col in table.columns:
        if col.align not in TABLE_ALIGNMENTS:
            raise EditorError(
                f"invalid column alignment {col.align!r} "
                f"(expected one of {sorted(TABLE_ALIGNMENTS)})"
            )
    for r, row in enumerate(table.rows):
        for cell in row.cells:
            if cell.colspan < 1 or cell.rowspan < 1:
                raise EditorError(
                    f"row {r}: cell spans must be >= 1 "
                    f"(got colspan={cell.colspan}, rowspan={cell.rowspan})"
                )
    problems = table_geometry_errors(table)
    if problems:
        raise EditorError("inconsistent table geometry: " + "; ".join(problems))


def _build_table_element(table: Table) -> etree._Element:
    """Serialize a Table into a fresh `<table>` lxml element.

    `colspan`/`rowspan` attributes are emitted only when > 1 (minimal diffs);
    empty cells serialize as self-closing `<cell/>`.
    """
    table_el = etree.Element("table")
    cols_el = etree.SubElement(table_el, "columns")
    for col in table.columns:
        etree.SubElement(cols_el, "col", align=col.align)
    for row in table.rows:
        row_attrib = {"header": "true"} if row.header else {}
        row_el = etree.SubElement(table_el, "row", row_attrib)
        for cell in row.cells:
            cell_attrib: dict[str, str] = {}
            if cell.colspan != 1:
                cell_attrib["colspan"] = str(cell.colspan)
            if cell.rowspan != 1:
                cell_attrib["rowspan"] = str(cell.rowspan)
            cell_el = etree.SubElement(row_el, "cell", cell_attrib)
            cell_el.text = _sanitize_text(cell.text or "", "table cell") or None
    return table_el


@_atomic
def set_table(folder: Path, obj_id: str, table: Table) -> None:
    """Replace (or create) an object's `<table>`, placed after `<description>`.

    Validates alignments, spans, and cell tiling first (raising EditorError on
    any inconsistency), so a malformed table never reaches disk.
    """
    doc = load_document(folder)
    if obj_id not in doc.objects:
        raise EditorError(f"no object {obj_id!r} in document {doc.id!r}")

    _validate_table(table)

    obj_path = folder / "objects" / f"{obj_id}.xml"
    tree = etree.parse(str(obj_path))
    root = tree.getroot()

    for existing in root.findall("table"):
        root.remove(existing)

    table_el = _build_table_element(table)
    description = root.find("description")
    if description is not None:
        description.addnext(table_el)
    else:
        root.append(table_el)

    etree.indent(tree, space="  ")
    _write_tree(tree, obj_path)


@_atomic
def clear_table(folder: Path, obj_id: str) -> bool:
    """Remove an object's `<table>` if present. Returns True iff one was removed."""
    doc = load_document(folder)
    if obj_id not in doc.objects:
        raise EditorError(f"no object {obj_id!r} in document {doc.id!r}")

    obj_path = folder / "objects" / f"{obj_id}.xml"
    tree = etree.parse(str(obj_path))
    root = tree.getroot()
    tables = root.findall("table")
    if not tables:
        return False
    for t in tables:
        root.remove(t)
    etree.indent(tree, space="  ")
    _write_tree(tree, obj_path)
    return True


def figure_path(folder: Path, obj_id: str) -> Path:
    """Return the on-disk path where `obj_id`'s figure would live (whether or not it exists)."""
    return _child_path(folder / "figures", f"{obj_id}.svg")


@_atomic
def set_figure(folder: Path, obj_id: str, src_path: Path) -> None:
    """Attach an SVG figure to `obj_id` by copying `src_path` into `figures/<obj_id>.svg`.

    Overwrites any existing figure for the object. The source must be a
    well-formed SVG file — we parse it to reject mis-named binaries (a PNG
    renamed to .svg, drawio's compressed native format) and broken XML
    before touching the destination.
    """
    src_path = Path(src_path)
    if src_path.suffix.lower() != ".svg":
        raise EditorError(
            f"figure source must be a .svg file, got {src_path.name!r}"
        )
    if not src_path.is_file():
        raise EditorError(f"figure source does not exist: {src_path}")

    doc = load_document(folder)
    if obj_id not in doc.objects:
        raise EditorError(f"no object {obj_id!r} in document {doc.id!r}")

    try:
        src_root = etree.parse(str(src_path)).getroot()
    except etree.XMLSyntaxError as e:
        raise EditorError(
            f"figure source is not well-formed XML: {src_path} ({e})"
        ) from e
    if etree.QName(src_root.tag).localname != "svg":
        raise EditorError(
            f"figure source root must be <svg>, got <{src_root.tag}>: {src_path}"
        )

    dest = figure_path(folder, obj_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _touch(dest)
    shutil.copyfile(src_path, dest)


@_atomic
def clear_figure(folder: Path, obj_id: str) -> bool:
    """Remove `obj_id`'s figure file if it exists. Returns True iff a file was removed.

    Raises EditorError if `obj_id` is not an object of the document.

    Cleans up the `figures/` directory when it becomes empty so the working
    tree doesn't carry an empty folder around (mirrors how `remove_link` cleans
    up empty `links/` files).
    """
    doc = load_document(folder)
    if obj_id not in doc.objects:
        raise EditorError(f"no object {obj_id!r} in document {doc.id!r}")
    return _remove_figure_file(figure_path(folder, obj_id))


def _remove_figure_file(path: Path) -> bool:
    if not path.is_file():
        return False
    _unlink(path)
    figures_dir = path.parent
    try:
        next(figures_dir.iterdir())
    except StopIteration:
        figures_dir.rmdir()
    return True


_METADATA_FIELDS = ("title", "author", "date")


@_atomic
def set_metadata(folder: Path, field: str, value: str) -> None:
    """Set one metadata field on the manifest. Creates the element if missing.

    `issue` is **not** a metadata field — it lives as a git tag
    (`<doc-id>-issue-<n>`). Use `git_ops.create_issue_tag` instead.
    """
    if field not in _METADATA_FIELDS:
        raise EditorError(
            f"unknown metadata field {field!r} "
            f"(known: {list(_METADATA_FIELDS)})"
        )
    value = _sanitize_text(value, f"metadata {field!r}")
    if field == "date" and value:
        try:
            _date.fromisoformat(value)
        except ValueError:
            raise EditorError(
                f"manifest metadata: date={value!r} is not "
                f"an ISO 8601 date (YYYY-MM-DD)"
            )
    doc = load_document(folder)
    manifest = folder / f"{doc.id}.xml"
    tree = etree.parse(str(manifest))
    root = tree.getroot()
    metadata = root.find("metadata")
    if metadata is None:
        metadata = etree.Element("metadata")
        root.insert(0, metadata)
    el = metadata.find(field)
    if el is None:
        el = etree.SubElement(metadata, field)
    el.text = value
    etree.indent(tree, space="  ")
    _write_tree(tree, manifest)


@_atomic
def add_parent(folder: Path, parent_doc: str) -> None:
    """Add a `<parent doc=...>` reference to the manifest's `<parents>`."""
    doc = load_document(folder)
    if parent_doc == doc.id:
        raise EditorError(f"cannot add {doc.id!r} as a parent of itself")
    if parent_doc in doc.parents:
        raise EditorError(f"parent {parent_doc!r} already declared")
    manifest = folder / f"{doc.id}.xml"
    tree = etree.parse(str(manifest))
    root = tree.getroot()
    parents_el = root.find("parents")
    if parents_el is None:
        parents_el = etree.Element("parents")
        metadata = root.find("metadata")
        if metadata is not None:
            metadata.addnext(parents_el)
        else:
            root.insert(0, parents_el)
    p = etree.SubElement(parents_el, "parent")
    p.set("doc", parent_doc)
    etree.indent(tree, space="  ")
    _write_tree(tree, manifest)


@_atomic
def remove_parent(folder: Path, parent_doc: str) -> bool:
    """Remove a `<parent doc=...>` from the manifest. Returns True if removed.

    If `<parents>` becomes empty, it is deleted.
    """
    doc = load_document(folder)
    manifest = folder / f"{doc.id}.xml"
    tree = etree.parse(str(manifest))
    root = tree.getroot()
    parents_el = root.find("parents")
    if parents_el is None:
        return False
    removed = False
    for p in list(parents_el.findall("parent")):
        if p.get("doc") == parent_doc:
            parents_el.remove(p)
            removed = True
    if not removed:
        return False
    if not parents_el.findall("parent"):
        root.remove(parents_el)
    etree.indent(tree, space="  ")
    _write_tree(tree, manifest)
    return True


@_atomic
def add_attr_option(folder: Path, attr_name: str, option: str) -> None:
    """Append a new enum option to an existing enum attribute."""
    option = _sanitize_text(option, "enum option")
    doc = load_document(folder)
    attr = doc.attribute(attr_name)
    if attr is None:
        raise EditorError(f"attribute {attr_name!r} is not declared")
    if attr.kind != "enum":
        raise EditorError(
            f"attribute {attr_name!r} is {attr.kind!r}, not enum"
        )
    if option in attr.options:
        raise EditorError(
            f"option {option!r} already declared for {attr_name!r}"
        )
    manifest = folder / f"{doc.id}.xml"
    tree = etree.parse(str(manifest))
    root = tree.getroot()
    attrs_el = root.find("attributes")
    if attrs_el is None:
        raise EditorError("manifest has no <attributes>")
    attr_el = None
    for a in attrs_el.findall("attribute"):
        if a.get("name") == attr_name:
            attr_el = a
            break
    if attr_el is None:
        raise EditorError(
            f"<attribute name={attr_name!r}> not found in manifest"
        )
    opt = etree.SubElement(attr_el, "option")
    opt.text = option
    etree.indent(tree, space="  ")
    _write_tree(tree, manifest)


_ATTR_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ATTR_KINDS = ("enum", "text", "integer", "real", "boolean", "date")


@_atomic
def add_attribute(
    folder: Path,
    name: str,
    kind: str,
    required: bool,
    options: tuple[str, ...] = (),
    label: str = "",
) -> None:
    """Declare a new attribute on a document's manifest schema."""
    if not name or not _ATTR_NAME_RE.match(name):
        raise EditorError(
            f"invalid attribute name {name!r}: "
            "use letters, digits, hyphens, underscores"
        )
    if kind not in _ATTR_KINDS:
        raise EditorError(
            f"invalid kind {kind!r} (expected one of {list(_ATTR_KINDS)})"
        )
    if kind != "enum" and options:
        raise EditorError(f"{kind} attributes cannot have options")
    label = _sanitize_text(label, "attribute label")
    options = tuple(_sanitize_text(opt, "enum option") for opt in options)
    if kind == "enum":
        seen: set[str] = set()
        for opt in options:
            if not opt:
                raise EditorError("enum option cannot be empty")
            if opt in seen:
                raise EditorError(f"duplicate option {opt!r}")
            seen.add(opt)

    doc = load_document(folder)
    if doc.attribute(name) is not None:
        raise EditorError(f"attribute {name!r} already declared")

    manifest = folder / f"{doc.id}.xml"
    tree = etree.parse(str(manifest))
    root = tree.getroot()
    attrs_el = root.find("attributes")
    if attrs_el is None:
        attrs_el = etree.SubElement(root, "attributes")
    attribs: dict[str, str] = {
        "name": name,
        "kind": kind,
        "required": "true" if required else "false",
    }
    if label:
        attribs["label"] = label
    attr_el = etree.SubElement(attrs_el, "attribute", **attribs)
    if kind == "enum":
        for opt in options:
            etree.SubElement(attr_el, "option").text = opt
    etree.indent(tree, space="  ")
    _write_tree(tree, manifest)


@_atomic
def change_attribute_required(folder: Path, name: str, required: bool) -> None:
    """Flip the `required` flag on an existing attribute.

    Idempotent. Flipping `false → true` succeeds even when existing objects do
    not yet carry the attribute — `validate` will surface those as
    `ATTR_MISSING_REQUIRED` so the caller can decide whether to backfill.
    """
    doc = load_document(folder)
    if doc.attribute(name) is None:
        raise EditorError(f"attribute {name!r} not declared")

    manifest = folder / f"{doc.id}.xml"
    tree = etree.parse(str(manifest))
    root = tree.getroot()
    attrs_el = root.find("attributes")
    if attrs_el is None:
        raise EditorError("manifest has no <attributes>")
    target = None
    for a in attrs_el.findall("attribute"):
        if a.get("name") == name:
            target = a
            break
    if target is None:
        raise EditorError(
            f"<attribute name={name!r}> not found in manifest"
        )
    target.set("required", "true" if required else "false")
    etree.indent(tree, space="  ")
    _write_tree(tree, manifest)


@_atomic
def delete_attribute(folder: Path, name: str) -> list[Path]:
    """Remove an attribute declaration and cascade to objects + rules.

    - Strips `<attribute name=...>` from the manifest.
    - Removes any `<attr name=...>` element from every object file.
    - Removes the corresponding directive from every rule; drops rules whose
      directives become empty as a result.

    Returns every file written: the manifest, then each changed object file
    and the rules file.
    """
    doc = load_document(folder)
    if doc.attribute(name) is None:
        raise EditorError(f"attribute {name!r} not declared")

    manifest = folder / f"{doc.id}.xml"
    tree = etree.parse(str(manifest))
    root = tree.getroot()
    attrs_el = root.find("attributes")
    removed_from_manifest = False
    if attrs_el is not None:
        for a in list(attrs_el.findall("attribute")):
            if a.get("name") == name:
                attrs_el.remove(a)
                removed_from_manifest = True
    if not removed_from_manifest:
        raise EditorError(
            f"<attribute name={name!r}> not found in manifest"
        )
    etree.indent(tree, space="  ")
    _write_tree(tree, manifest)
    written = [manifest]

    objects_dir = folder / "objects"
    if objects_dir.is_dir():
        for obj_path in sorted(objects_dir.glob("*.xml")):
            otree = etree.parse(str(obj_path))
            oroot = otree.getroot()
            changed = False
            for a in list(oroot.findall("attr")):
                if a.get("name") == name:
                    oroot.remove(a)
                    changed = True
            if changed:
                etree.indent(otree, space="  ")
                _write_tree(otree, obj_path)
                written.append(obj_path)

    rules_path = folder / f"{doc.id}.rules.xml"
    if rules_path.is_file():
        rtree = etree.parse(str(rules_path))
        rroot = rtree.getroot()
        rules_changed = False
        for rule_el in list(rroot.findall("rule")):
            then_el = rule_el.find("then")
            if then_el is None:
                continue
            for attr_el in list(then_el.findall("attr")):
                if attr_el.get("name") == name:
                    then_el.remove(attr_el)
                    rules_changed = True
            if not list(then_el.findall("attr")):
                rroot.remove(rule_el)
                rules_changed = True
        if rules_changed:
            etree.indent(rtree, space="  ")
            _write_tree(rtree, rules_path)
            written.append(rules_path)
    return written


@_atomic
def remove_attr_option(folder: Path, attr_name: str, option: str) -> list[Path]:
    """Remove an enum option from a manifest attribute and blank matching values.

    - Strips `<option>` from the attribute in the manifest.
    - For every object whose `<attr name=attr_name>` text equals `option`,
      clears the text (the element stays so the user can see the now-blank
      value rather than silently lose it; the validator will surface the
      missing-required state).

    Returns every file written: the manifest, then each changed object file.
    """
    doc = load_document(folder)
    attr = doc.attribute(attr_name)
    if attr is None:
        raise EditorError(f"attribute {attr_name!r} not declared")
    if attr.kind != "enum":
        raise EditorError(
            f"attribute {attr_name!r} is {attr.kind!r}, not enum"
        )
    if option not in attr.options:
        raise EditorError(
            f"option {option!r} not declared for {attr_name!r}"
        )

    manifest = folder / f"{doc.id}.xml"
    tree = etree.parse(str(manifest))
    root = tree.getroot()
    attrs_el = root.find("attributes")
    if attrs_el is None:
        raise EditorError("manifest has no <attributes>")
    attr_el = None
    for a in attrs_el.findall("attribute"):
        if a.get("name") == attr_name:
            attr_el = a
            break
    if attr_el is None:
        raise EditorError(
            f"<attribute name={attr_name!r}> not found in manifest"
        )
    removed = False
    for opt in list(attr_el.findall("option")):
        if (opt.text or "") == option:
            attr_el.remove(opt)
            removed = True
    if not removed:
        raise EditorError(
            f"<option>{option}</option> not found under {attr_name!r}"
        )
    etree.indent(tree, space="  ")
    _write_tree(tree, manifest)
    written = [manifest]

    objects_dir = folder / "objects"
    if objects_dir.is_dir():
        for obj_path in sorted(objects_dir.glob("*.xml")):
            otree = etree.parse(str(obj_path))
            oroot = otree.getroot()
            changed = False
            for a in oroot.findall("attr"):
                if a.get("name") == attr_name and (a.text or "") == option:
                    a.text = ""
                    changed = True
            if changed:
                etree.indent(otree, space="  ")
                _write_tree(otree, obj_path)
                written.append(obj_path)
    return written


@_atomic
def move_object(
    folder: Path,
    obj_id: str,
    *,
    parent_id: Optional[str] = None,
    to_top: bool = False,
    before: Optional[str] = None,
    after: Optional[str] = None,
) -> None:
    """Reparent and/or reorder an object in the manifest outline.

    Exactly one positioning intent is required (at least one of `parent_id`,
    `to_top`, `before`, `after`). `parent_id` and `to_top` are mutually
    exclusive, as are `before` and `after`.

    `before`/`after` position the ref adjacent to a sibling under the target
    parent. Without either, the ref is appended as the last child. Moves
    preserve the ref's subtree — children travel with their parent.
    """
    if parent_id is not None and to_top:
        raise EditorError("cannot combine --parent with --top")
    if before is not None and after is not None:
        raise EditorError("cannot combine --before with --after")
    if parent_id is None and not to_top and before is None and after is None:
        raise EditorError(
            "specify at least one of --parent, --top, --before, --after"
        )

    doc = load_document(folder)
    if obj_id not in doc.objects:
        raise EditorError(f"no object {obj_id!r} in document {doc.id!r}")

    manifest = folder / f"{doc.id}.xml"
    tree = etree.parse(str(manifest))
    root = tree.getroot()
    outline = root.find("outline")
    if outline is None:
        raise EditorError("manifest has no <outline>")

    ref_el = _find_ref(outline, obj_id)
    if ref_el is None:
        raise EditorError(f"{obj_id!r} not in outline")

    if to_top:
        target_parent = outline
    elif parent_id is not None:
        if parent_id == obj_id:
            raise EditorError(f"cannot move {obj_id!r} under itself")
        if _find_ref(ref_el, parent_id) is not None:
            raise EditorError(
                f"cannot move {obj_id!r} under its own descendant {parent_id!r}"
            )
        found = _find_ref(outline, parent_id)
        if found is None:
            raise EditorError(f"parent {parent_id!r} not found in outline")
        target_parent = found
    else:
        target_parent = ref_el.getparent()

    anchor = None
    if before is not None or after is not None:
        anchor_id = before if before is not None else after
        for r in target_parent.findall("ref"):
            if r.get("id") == anchor_id:
                anchor = r
                break
        if anchor is None:
            raise EditorError(
                f"sibling {anchor_id!r} not found under target parent"
            )
        if anchor is ref_el:
            raise EditorError(
                f"cannot position {obj_id!r} relative to itself"
            )

    _detach_ref(ref_el)

    if anchor is None:
        target_parent.append(ref_el)
    else:
        idx = list(target_parent).index(anchor)
        if after is not None:
            idx += 1
        target_parent.insert(idx, ref_el)

    etree.indent(tree, space="  ")
    _write_tree(tree, manifest)


@_atomic
def set_outline(folder: Path, ordering: list[dict]) -> None:
    """Rewrite the manifest's `<outline>` to match `ordering` in one pass.

    `ordering` is a list of `{"id": str, "children": list[dict]}` dicts —
    the same shape `serialize.outline_node_to_dict` produces, so an agent
    can `show_document(..., include=["outline"])` + mutate + send back.

    Every object in `doc.objects` must appear in `ordering` exactly once.
    Missing, extra, or duplicate ids all raise `EditorError`. This is the
    atomic-restructure use case — patch-style reorders should use
    `move_object` instead.
    """
    doc = load_document(folder)
    seen: set[str] = set()

    def collect(items: list[dict]) -> None:
        for item in items:
            oid = item.get("id")
            if not isinstance(oid, str) or not oid:
                raise EditorError(
                    "ordering entries must have a non-empty 'id' string"
                )
            if oid in seen:
                raise EditorError(
                    f"object {oid!r} appears multiple times in ordering"
                )
            seen.add(oid)
            collect(item.get("children", []) or [])

    collect(ordering)

    extra = sorted(seen - set(doc.objects))
    if extra:
        raise EditorError(
            f"ordering references unknown objects: {extra}"
        )
    missing = sorted(set(doc.objects) - seen)
    if missing:
        raise EditorError(f"ordering omits objects: {missing}")

    manifest = folder / f"{doc.id}.xml"
    tree = etree.parse(str(manifest))
    root = tree.getroot()
    outline = root.find("outline")
    if outline is None:
        raise EditorError("manifest has no <outline>")

    for child in list(outline):
        outline.remove(child)

    def build(parent_el, items: list[dict]) -> None:
        for item in items:
            ref = etree.SubElement(parent_el, "ref", id=item["id"])
            children = item.get("children", []) or []
            if children:
                build(ref, children)

    build(outline, ordering)

    etree.indent(tree, space="  ")
    _write_tree(tree, manifest)


@_atomic
def add_link(
    folder: Path,
    source_id: str,
    target_doc: str,
    target_id: str,
    link_type: str,
) -> str | None:
    """Add an outgoing link from `source_id` to `target_doc/target_id`.

    If a git repo is available and the target file is committed, @ack is set
    to the target's current last-commit short hash (link starts OK). Otherwise
    @ack is omitted (link starts suspect — never reviewed).

    Returns the ack value written, or None if no ack was set.
    Raises if the link already exists (same target-doc + target-id on source).
    """
    link_type = _sanitize_text(link_type, "link type")
    doc = load_document(folder)
    if source_id not in doc.objects:
        raise EditorError(f"no object {source_id!r} in document {doc.id!r}")

    if target_doc != doc.id and target_doc not in doc.parents:
        raise EditorError(
            f"{doc.id!r} is not allowed to link to {target_doc!r}; "
            f"declared parents are {list(doc.parents)} "
            f"(add a <parent doc={target_doc!r}/> first if intentional)"
        )

    ack = _compute_ack(folder, target_doc, target_id)

    links_path = folder / "links" / f"{source_id}.xml"
    if links_path.exists():
        tree = etree.parse(str(links_path))
        root = tree.getroot()
        for existing in root.findall("link"):
            if (
                existing.get("target-doc") == target_doc
                and existing.get("target-id") == target_id
            ):
                raise EditorError(
                    f"link already exists: {source_id} -> {target_doc}/{target_id}"
                )
    else:
        root = etree.Element("links", source=source_id)
        tree = etree.ElementTree(root)
    link_el = etree.SubElement(root, "link")
    link_el.set("target-doc", target_doc)
    link_el.set("target-id", target_id)
    link_el.set("type", link_type)
    if ack is not None:
        link_el.set("ack", ack)
    etree.indent(tree, space="  ")
    links_path.parent.mkdir(parents=True, exist_ok=True)
    _write_tree(tree, links_path)

    return ack


@dataclass(frozen=True, slots=True)
class DeleteReport:
    """Result of a `delete_object` call.

    `performed` is False only when the delete was blocked by incoming links
    and `force=False`. In that case no files are touched and `incoming` lists
    the blockers. Otherwise `incoming` lists what was stripped, and
    `removed_files` / `modified_files` record the full working-copy footprint.
    """

    obj_id: str
    # (source_doc_id, source_object_id, link_type) for each incoming link.
    incoming: tuple[tuple[str, str, str], ...]
    removed_files: tuple[Path, ...]
    modified_files: tuple[Path, ...]
    performed: bool


@_atomic
def delete_object(
    folder: Path,
    obj_id: str,
    *,
    force: bool = False,
) -> DeleteReport:
    """Hard-delete an object: file, figure, outline ref, own outgoing links,
    and every incoming link across all sibling docs.

    Refuses (returning a report with `performed=False`) when incoming links
    exist and `force=False`. Always refuses if the object has children in the
    outline — callers must re-parent or delete the children first. Relies on
    `load_sibling_docs` to discover cross-doc incoming links; works in or out
    of a git repo. Raises `EditorError`, even with `force=True`, while any
    sibling doc fails to load, since its links can't be checked or stripped.
    """
    doc = load_document(folder)
    if obj_id not in doc.objects:
        raise EditorError(f"no object {obj_id!r} in document {doc.id!r}")

    manifest = folder / f"{doc.id}.xml"
    tree = etree.parse(str(manifest))
    root = tree.getroot()
    outline = root.find("outline")
    ref_el = _find_ref(outline, obj_id) if outline is not None else None
    if ref_el is None:
        raise EditorError(f"{obj_id!r} not in outline")
    if len(ref_el.findall("ref")) > 0:
        raise EditorError(
            f"{obj_id!r} has children in the outline — "
            f"move or delete them first"
        )

    load_errors: list[DocLoadError] = []
    docs = load_sibling_docs(folder, doc, load_errors=load_errors)
    if load_errors:
        failed = "; ".join(f"{e.doc_id}: {e.message}" for e in load_errors)
        raise EditorError(
            f"cannot delete {obj_id!r} while a document fails to load, "
            f"because its links to {obj_id!r} can't be checked — {failed}"
        )
    incoming: list[tuple[Path, str, str, str]] = []  # (src_folder, src_doc, src_id, type)
    for src_doc_id, loaded in docs.items():
        for source_id, link_list in loaded.document.links.items():
            for link in link_list:
                if link.target_doc == doc.id and link.target_id == obj_id:
                    incoming.append(
                        (loaded.folder, src_doc_id, source_id, link.type)
                    )
    incoming_summary = tuple(
        (src_doc, src_id, ltype) for _, src_doc, src_id, ltype in incoming
    )

    if incoming and not force:
        return DeleteReport(
            obj_id=obj_id,
            incoming=incoming_summary,
            removed_files=(),
            modified_files=(),
            performed=False,
        )

    removed: list[Path] = []
    modified: list[Path] = []

    # 1. Figure, so a later object that re-uses this id doesn't inherit it.
    #    Done first: if the SVG can't be removed (a viewer holding it open),
    #    nothing else has been touched yet.
    fig_path = figure_path(folder, obj_id)
    if _remove_figure_file(fig_path):
        removed.append(fig_path)

    # 2. Strip incoming links. remove_link() removes every <link> with the
    #    matching target and deletes the file if it becomes empty, so one call
    #    per (src_folder, src_id) is enough.
    seen: set[tuple[str, str]] = set()
    for src_folder, _src_doc, src_id, _ltype in incoming:
        key = (str(src_folder.resolve()), src_id)
        if key in seen:
            continue
        seen.add(key)
        src_links_path = src_folder / "links" / f"{src_id}.xml"
        remove_link(src_folder, src_id, doc.id, obj_id)
        if src_links_path.exists():
            modified.append(src_links_path)
        else:
            removed.append(src_links_path)

    # 3. Victim's own outgoing-links file.
    own_links = folder / "links" / f"{obj_id}.xml"
    if own_links.exists():
        _unlink(own_links)
        removed.append(own_links)

    # 4. Object file.
    obj_path = folder / "objects" / f"{obj_id}.xml"
    _unlink(obj_path)
    removed.append(obj_path)

    # 5. Outline ref.
    _detach_ref(ref_el)
    etree.indent(tree, space="  ")
    _write_tree(tree, manifest)
    modified.append(manifest)

    return DeleteReport(
        obj_id=obj_id,
        incoming=incoming_summary,
        removed_files=tuple(removed),
        modified_files=tuple(modified),
        performed=True,
    )


@_atomic
def remove_link(
    folder: Path, source_id: str, target_doc: str, target_id: str
) -> bool:
    """Remove an outgoing link. Returns True if a link was removed.

    If the links file becomes empty, it is deleted.
    """
    links_path = _child_path(folder / "links", f"{source_id}.xml")
    if not links_path.exists():
        return False

    tree = etree.parse(str(links_path))
    root = tree.getroot()

    removed = False
    for existing in list(root.findall("link")):
        if (
            existing.get("target-doc") == target_doc
            and existing.get("target-id") == target_id
        ):
            root.remove(existing)
            removed = True

    if not removed:
        return False

    remaining = root.findall("link")
    if not remaining:
        _unlink(links_path)
    else:
        etree.indent(tree, space="  ")
        _write_tree(tree, links_path)
    return True


_VIEW_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_view_name(name: str) -> str:
    slug = _VIEW_SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug


# Exactly the shapes `_slugify_view_name` (plus a `-N` suffix) can produce.
_SLUG_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


def _slug_path(folder: Path, subdir: str, slug: str) -> Path:
    """Return `folder/<subdir>/<slug>.xml`, refusing a slug that isn't one."""
    if not _SLUG_RE.fullmatch(slug or ""):
        raise EditorError(
            f"invalid slug {slug!r}: use lowercase letters, digits and single hyphens"
        )
    return _child_path(folder / subdir, f"{slug}.xml")


@_atomic
def create_view(
    folder: Path,
    name: str,
    visible_attrs: tuple[str, ...] | list[str],
) -> str:
    """Save a new attribute-visibility view as `views/<slug>.xml`. Returns the slug.

    `name` is the human-readable view name; the file stem is a slug derived
    from it. If a view with the same slug already exists, suffixes `-2`, `-3`…
    are tried until a free slug is found.
    """
    name = _sanitize_text(name or "", "view name").strip()
    if not name:
        raise EditorError("view name cannot be empty")
    base_slug = _slugify_view_name(name)
    if not base_slug:
        raise EditorError(
            f"view name {name!r} produces an empty slug; "
            "use letters or digits"
        )

    views_dir = folder / "views"
    views_dir.mkdir(parents=True, exist_ok=True)

    slug = base_slug
    n = 2
    while (views_dir / f"{slug}.xml").exists():
        slug = f"{base_slug}-{n}"
        n += 1

    seen: set[str] = set()
    deduped: list[str] = []
    for attr in visible_attrs:
        if attr in seen:
            continue
        seen.add(attr)
        deduped.append(attr)

    root_el = etree.Element("view", name=name)
    for attr in deduped:
        etree.SubElement(root_el, "attribute", name=attr)
    tree = etree.ElementTree(root_el)
    etree.indent(tree, space="  ")
    _write_tree(tree, views_dir / f"{slug}.xml")
    return slug


@_atomic
def delete_view(folder: Path, slug: str) -> None:
    """Delete `views/<slug>.xml`. Raises if the file does not exist."""
    path = _slug_path(folder, "views", slug)
    if not path.is_file():
        raise EditorError(f"view {slug!r} not found at {path}")
    _unlink(path)


@_atomic
def rename_view(folder: Path, slug: str, new_name: str) -> str:
    """Rename a view's display name (and re-slug the file if needed).

    Returns the new slug. If `new_name` slugifies to the same value as the
    existing slug, only the `<view name=...>` attribute is updated. Otherwise
    the file is renamed too (with `-2`, `-3`… suffix on collision).
    """
    new_name = _sanitize_text(new_name or "", "view name").strip()
    if not new_name:
        raise EditorError("view name cannot be empty")
    src = _slug_path(folder, "views", slug)
    if not src.is_file():
        raise EditorError(f"view {slug!r} not found at {src}")

    base_slug = _slugify_view_name(new_name)
    if not base_slug:
        raise EditorError(
            f"view name {new_name!r} produces an empty slug; "
            "use letters or digits"
        )

    new_slug = base_slug
    if new_slug != slug:
        n = 2
        while (folder / "views" / f"{new_slug}.xml").exists():
            new_slug = f"{base_slug}-{n}"
            n += 1

    tree = etree.parse(str(src))
    root = tree.getroot()
    root.set("name", new_name)
    etree.indent(tree, space="  ")

    if new_slug == slug:
        _write_tree(tree, src)
    else:
        dst = folder / "views" / f"{new_slug}.xml"
        _write_tree(tree, dst)
        _unlink(src)
    return new_slug


@_atomic
def set_view_visible(
    folder: Path,
    slug: str,
    visible_attrs: tuple[str, ...] | list[str],
) -> None:
    """Replace the visible-attribute list of an existing view, preserving order.

    Rewrites `views/<slug>.xml` so its `<attribute name=...>` children match
    `visible_attrs` exactly, in the given order. Duplicate names are kept only
    on first occurrence. The view's `<view name=...>` display name is preserved.
    """
    path = _slug_path(folder, "views", slug)
    if not path.is_file():
        raise EditorError(f"view {slug!r} not found at {path}")

    seen: set[str] = set()
    deduped: list[str] = []
    for attr in visible_attrs:
        if attr in seen:
            continue
        seen.add(attr)
        deduped.append(attr)

    tree = etree.parse(str(path))
    root = tree.getroot()
    for existing in list(root.findall("attribute")):
        root.remove(existing)
    for attr in deduped:
        etree.SubElement(root, "attribute", name=attr)
    etree.indent(tree, space="  ")
    _write_tree(tree, path)


# --- filters --------------------------------------------------------------
#
# Mirror of the views CRUD above. Filters live as standalone files under
# `filters/<slug>.xml` and are not coupled to any view; the GUI applies a
# (View, Filter) pair independently.


_ALL_ATTR_OPS = frozenset(
    op
    for ops in (
        valid_ops_for_kind("enum"),
        valid_ops_for_kind("text"),
        valid_ops_for_kind("integer"),
        valid_ops_for_kind("real"),
        valid_ops_for_kind("boolean"),
        valid_ops_for_kind("date"),
    )
    for op in ops
)


def _validate_rules(rules: tuple[FilterRule, ...] | list[FilterRule]) -> None:
    for rule in rules:
        if rule.kind not in KINDS:
            raise EditorError(
                f"rule kind must be one of {KINDS}, got {rule.kind!r}"
            )
        if rule.kind == "attr":
            if not rule.attr:
                raise EditorError("attr rule missing attribute name")
            if rule.op and rule.op not in _ALL_ATTR_OPS:
                raise EditorError(
                    f"attr rule op {rule.op!r} is not a recognised operator"
                )
        else:
            if rule.op not in LINK_OPS:
                raise EditorError(
                    f"{rule.kind} op must be one of {LINK_OPS}, got {rule.op!r}"
                )


def _write_filter(
    path: Path,
    name: str,
    combinator: str,
    keep_ancestors: bool,
    rules: tuple[FilterRule, ...] | list[FilterRule],
) -> None:
    if combinator not in COMBINATORS:
        raise EditorError(
            f"combinator must be one of {COMBINATORS}, got {combinator!r}"
        )
    _validate_rules(rules)
    root_el = etree.Element(
        "filter",
        name=name,
        combinator=combinator,
    )
    root_el.set("keep-ancestors", "true" if keep_ancestors else "false")
    for rule in rules:
        attrs = {"kind": rule.kind, "op": rule.op}
        if rule.attr:
            attrs["attr"] = rule.attr
        if rule.value:
            attrs["value"] = _sanitize_text(rule.value, "filter value")
        etree.SubElement(root_el, "rule", **attrs)
    tree = etree.ElementTree(root_el)
    etree.indent(tree, space="  ")
    _write_tree(tree, path)


@_atomic
def create_filter(
    folder: Path,
    name: str,
    combinator: str = "all",
    keep_ancestors: bool = True,
    rules: tuple[FilterRule, ...] | list[FilterRule] = (),
) -> str:
    """Save a new filter as `filters/<slug>.xml`. Returns the slug.

    `name` is the human-readable filter name; the file stem is a slug derived
    from it. If a filter with the same slug already exists, suffixes `-2`, `-3`…
    are tried until a free slug is found.
    """
    name = _sanitize_text(name or "", "filter name").strip()
    if not name:
        raise EditorError("filter name cannot be empty")
    base_slug = _slugify_view_name(name)
    if not base_slug:
        raise EditorError(
            f"filter name {name!r} produces an empty slug; "
            "use letters or digits"
        )

    filters_dir = folder / "filters"
    filters_dir.mkdir(parents=True, exist_ok=True)

    slug = base_slug
    n = 2
    while (filters_dir / f"{slug}.xml").exists():
        slug = f"{base_slug}-{n}"
        n += 1

    _write_filter(
        filters_dir / f"{slug}.xml",
        name=name,
        combinator=combinator,
        keep_ancestors=keep_ancestors,
        rules=rules,
    )
    return slug


@_atomic
def delete_filter(folder: Path, slug: str) -> None:
    """Delete `filters/<slug>.xml`. Raises if the file does not exist."""
    path = _slug_path(folder, "filters", slug)
    if not path.is_file():
        raise EditorError(f"filter {slug!r} not found at {path}")
    _unlink(path)


@_atomic
def rename_filter(folder: Path, slug: str, new_name: str) -> str:
    """Rename a filter's display name (and re-slug the file if needed).

    Returns the new slug. If `new_name` slugifies to the same value as the
    existing slug, only the `<filter name=...>` attribute is updated. Otherwise
    the file is renamed too (with `-2`, `-3`… suffix on collision).
    """
    new_name = _sanitize_text(new_name or "", "filter name").strip()
    if not new_name:
        raise EditorError("filter name cannot be empty")
    src = _slug_path(folder, "filters", slug)
    if not src.is_file():
        raise EditorError(f"filter {slug!r} not found at {src}")

    base_slug = _slugify_view_name(new_name)
    if not base_slug:
        raise EditorError(
            f"filter name {new_name!r} produces an empty slug; "
            "use letters or digits"
        )

    new_slug = base_slug
    if new_slug != slug:
        n = 2
        while (folder / "filters" / f"{new_slug}.xml").exists():
            new_slug = f"{base_slug}-{n}"
            n += 1

    tree = etree.parse(str(src))
    root = tree.getroot()
    root.set("name", new_name)
    etree.indent(tree, space="  ")

    if new_slug == slug:
        _write_tree(tree, src)
    else:
        dst = folder / "filters" / f"{new_slug}.xml"
        _write_tree(tree, dst)
        _unlink(src)
    return new_slug


@_atomic
def set_filter_rules(
    folder: Path,
    slug: str,
    combinator: str,
    keep_ancestors: bool,
    rules: tuple[FilterRule, ...] | list[FilterRule],
) -> None:
    """Rewrite an existing filter's combinator + keep-ancestors + rule list.

    The `<filter name=...>` display name is preserved.
    """
    path = _slug_path(folder, "filters", slug)
    if not path.is_file():
        raise EditorError(f"filter {slug!r} not found at {path}")
    if combinator not in COMBINATORS:
        raise EditorError(
            f"combinator must be one of {COMBINATORS}, got {combinator!r}"
        )
    _validate_rules(rules)

    tree = etree.parse(str(path))
    root = tree.getroot()
    name = root.get("name") or ""
    _write_filter(
        path,
        name=name,
        combinator=combinator,
        keep_ancestors=keep_ancestors,
        rules=rules,
    )


def _add_outline_ref(
    manifest: Path, new_id: str, parent_id: Optional[str]
) -> None:
    tree = etree.parse(str(manifest))
    root = tree.getroot()
    outline = root.find("outline")
    if outline is None:
        outline = etree.SubElement(root, "outline")

    if parent_id is None:
        parent_el = outline
    else:
        parent_el = _find_ref(outline, parent_id)
        if parent_el is None:
            raise EditorError(
                f"parent {parent_id!r} not found in outline of {manifest.name}"
            )

    new_ref = etree.SubElement(parent_el, "ref")
    new_ref.set("id", new_id)
    etree.indent(tree, space="  ")
    _write_tree(tree, manifest)


def _detach_ref(ref_el: etree._Element) -> None:
    """Remove an outline ref, dropping the indent whitespace an emptied parent keeps.

    `etree.indent` leaves a childless element's text alone, so without this the
    old parent would serialize as `<ref id="X">\\n    </ref>`.
    """
    parent = ref_el.getparent()
    parent.remove(ref_el)
    if len(parent) == 0 and not (parent.text or "").strip():
        parent.text = None


def _find_ref(parent: etree._Element, ref_id: str) -> etree._Element | None:
    for r in parent.findall("ref"):
        if r.get("id") == ref_id:
            return r
        found = _find_ref(r, ref_id)
        if found is not None:
            return found
    return None


def _compute_ack(folder: Path, target_doc: str, target_id: str) -> str | None:
    try:
        repo, repo_root = discover_repo(folder)
    except GitError:
        return None
    rel = f"{target_doc}/objects/{target_id}.xml"
    target_path = repo_root / rel
    if not target_path.is_file():
        return None
    # Same paths and rule as the suspect scan, so a new link starts OK.
    fig_rel = f"{target_doc}/figures/{target_id}.svg"
    return last_commit_for_paths(repo, [rel, fig_rel])

"""Parse a document folder into the in-memory Document model.

Folder layout (see PLAN.md):
    <doc_id>/
        <doc_id>.xml       # manifest
        objects/*.xml      # one file per object
        links/*.xml        # one file per source object (optional)
        views/*.xml        # one file per saved attribute-visibility view (optional)
        filters/*.xml      # one file per saved filter (optional)
        figures/*.svg      # one figure per object (optional, max one per id)
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from lxml import etree

from .schema import (
    TABLE_ALIGNMENTS,
    Attribute,
    Document,
    Filter,
    FilterRule,
    Link,
    LinkDirective,
    Metadata,
    OutlineNode,
    RequirementObject,
    Rule,
    Table,
    TableCell,
    TableColumn,
    TableRow,
    View,
    table_geometry_errors,
)
from .filter import COMBINATORS, KINDS, LINK_OPS, valid_ops_for_kind


class LoaderError(Exception):
    """Raised when the on-disk files can't be parsed into a Document."""


def load_document(folder: str | Path) -> Document:
    """Load the document rooted at `folder`.

    `folder` name must equal the manifest's `id` attribute, and the manifest
    filename must be `<folder_name>.xml` inside that folder.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise LoaderError(f"Not a directory: {folder}")

    doc_id = folder.name
    manifest_path = folder / f"{doc_id}.xml"
    if not manifest_path.is_file():
        raise LoaderError(f"Manifest not found: {manifest_path}")

    manifest = _parse_xml(manifest_path)
    if manifest.tag != "document":
        raise LoaderError(
            f"{manifest_path}: root element must be <document>, got <{manifest.tag}>"
        )
    manifest_id = manifest.get("id")
    if manifest_id != doc_id:
        raise LoaderError(
            f"{manifest_path}: document id={manifest_id!r} does not match "
            f"folder name {doc_id!r}"
        )

    metadata = _parse_metadata(manifest.find("metadata"))
    parents = _parse_parents(manifest.find("parents"))
    attributes = _parse_attributes(manifest.find("attributes"))
    outline = _parse_outline(manifest.find("outline"))
    rules = _parse_rules_file(folder / f"{doc_id}.rules.xml")

    objects_dir = folder / "objects"
    objects: dict[str, RequirementObject] = {}
    if objects_dir.is_dir():
        for path in sorted(objects_dir.glob("*.xml")):
            obj = _parse_object(path)
            if obj.id in objects:
                raise LoaderError(f"Duplicate object id {obj.id!r} in {objects_dir}")
            objects[obj.id] = obj

    figures_dir = folder / "figures"
    if figures_dir.is_dir():
        figure_stems = {
            p.stem for p in figures_dir.glob("*.svg") if p.is_file()
        }
        for obj_id in figure_stems & objects.keys():
            objects[obj_id] = dataclasses.replace(
                objects[obj_id], figure_path=f"figures/{obj_id}.svg"
            )

    links_dir = folder / "links"
    links: dict[str, tuple[Link, ...]] = {}
    if links_dir.is_dir():
        for path in sorted(links_dir.glob("*.xml")):
            source, link_list = _parse_links(path)
            if source in links:
                raise LoaderError(f"Duplicate links source {source!r} in {links_dir}")
            links[source] = link_list

    views_dir = folder / "views"
    views: list[View] = []
    if views_dir.is_dir():
        seen_slugs: set[str] = set()
        for path in sorted(views_dir.glob("*.xml")):
            view = _parse_view(path)
            if view.slug in seen_slugs:
                raise LoaderError(f"Duplicate view slug {view.slug!r} in {views_dir}")
            seen_slugs.add(view.slug)
            views.append(view)

    filters_dir = folder / "filters"
    filters: list[Filter] = []
    if filters_dir.is_dir():
        seen_filter_slugs: set[str] = set()
        for path in sorted(filters_dir.glob("*.xml")):
            filt = _parse_filter(path)
            if filt.slug in seen_filter_slugs:
                raise LoaderError(
                    f"Duplicate filter slug {filt.slug!r} in {filters_dir}"
                )
            seen_filter_slugs.add(filt.slug)
            filters.append(filt)

    return Document(
        id=doc_id,
        metadata=metadata,
        parents=parents,
        attributes=attributes,
        outline=outline,
        objects=objects,
        links=links,
        rules=rules,
        views=tuple(views),
        filters=tuple(filters),
    )


def _parse_xml(path: Path) -> etree._Element:
    try:
        tree = etree.parse(str(path))
    except etree.XMLSyntaxError as e:
        raise LoaderError(f"{path}: XML parse error: {e}") from e
    except OSError as e:
        # Unreadable (locked, no permission, a directory named *.xml): one
        # broken document, not a reason to abort a repo-wide load.
        raise LoaderError(f"{path}: cannot read file: {e}") from e
    return tree.getroot()


def _text(elem: etree._Element | None) -> str:
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


def _parse_metadata(elem: etree._Element | None) -> Metadata:
    if elem is None:
        return Metadata()
    # `<issue>` is intentionally ignored — issues live as git tags
    # (`<doc-id>-issue-<n>`), not in the manifest. Tolerating its presence
    # here lets a manifest written before issue tags still load.
    return Metadata(
        title=_text(elem.find("title")),
        author=_text(elem.find("author")),
        date=_text(elem.find("date")),
    )


def _parse_parents(elem: etree._Element | None) -> tuple[str, ...]:
    if elem is None:
        return ()
    out: list[str] = []
    for p in elem.findall("parent"):
        doc = p.get("doc")
        if not doc:
            raise LoaderError("<parent> missing required @doc")
        out.append(doc)
    return tuple(out)


_ATTR_KINDS = ("enum", "text", "integer", "real", "boolean", "date")


def _parse_attributes(elem: etree._Element | None) -> tuple[Attribute, ...]:
    if elem is None:
        return ()
    out: list[Attribute] = []
    for a in elem.findall("attribute"):
        name = a.get("name")
        kind = a.get("kind")
        if not name or not kind:
            raise LoaderError("<attribute> requires @name and @kind")
        if kind not in _ATTR_KINDS:
            raise LoaderError(
                f"<attribute name={name!r}>: unsupported kind={kind!r} "
                f"(expected one of {list(_ATTR_KINDS)})"
            )
        required = a.get("required", "false").lower() == "true"
        label = a.get("label", "")
        options = tuple(_text(o) for o in a.findall("option"))
        if kind == "enum" and not options:
            raise LoaderError(f"<attribute name={name!r}> kind=enum has no <option>s")
        if kind != "enum" and options:
            raise LoaderError(
                f"<attribute name={name!r}> kind={kind} cannot declare <option>s"
            )
        out.append(Attribute(name=name, kind=kind, required=required, options=options, label=label))
    return tuple(out)


def _parse_outline(elem: etree._Element | None) -> tuple[OutlineNode, ...]:
    if elem is None:
        return ()
    return _parse_refs(elem)


def _parse_refs(parent: etree._Element) -> tuple[OutlineNode, ...]:
    out: list[OutlineNode] = []
    for r in parent.findall("ref"):
        ref_id = r.get("id")
        if not ref_id:
            raise LoaderError("<ref> missing required @id")
        out.append(OutlineNode(ref_id=ref_id, children=_parse_refs(r)))
    return tuple(out)


def _parse_object(path: Path) -> RequirementObject:
    root = _parse_xml(path)
    if root.tag != "object":
        raise LoaderError(f"{path}: root must be <object>, got <{root.tag}>")
    obj_id = root.get("id")
    if not obj_id:
        raise LoaderError(f"{path}: <object> missing required @id")
    if path.stem != obj_id:
        raise LoaderError(
            f"{path}: filename stem {path.stem!r} does not match @id={obj_id!r}"
        )

    attrs: dict[str, str] = {}
    for a in root.findall("attr"):
        name = a.get("name")
        if not name:
            raise LoaderError(f"{path}: <attr> missing required @name")
        if name in attrs:
            raise LoaderError(f"{path}: duplicate <attr name={name!r}>")
        attrs[name] = _text(a)

    description = _text(root.find("description"))
    table = _parse_table(root.find("table"), path)
    return RequirementObject(
        id=obj_id, attrs=attrs, description=description, table=table
    )


def _parse_span(value: str | None, attr: str, path: Path, obj_id: str) -> int:
    """Parse a colspan/rowspan attribute: a positive int, default 1."""
    if value is None:
        return 1
    try:
        span = int(value)
    except ValueError:
        raise LoaderError(
            f"{path}: <cell> @{attr}={value!r} is not an integer (object {obj_id})"
        )
    if span < 1:
        raise LoaderError(
            f"{path}: <cell> @{attr}={span} must be >= 1 (object {obj_id})"
        )
    return span


def _parse_table(elem: etree._Element | None, path: Path) -> Table | None:
    """Parse an `<object>`'s `<table>` child into a Table, or None if absent.

    Raises LoaderError on a bad alignment, a non-positive span, or a tiling
    that doesn't form a consistent rectangle — a corrupt table fails loudly at
    load, like a bad enum option does.
    """
    if elem is None:
        return None
    obj_id = path.stem

    columns: list[TableColumn] = []
    cols_el = elem.find("columns")
    if cols_el is not None:
        for c in cols_el.findall("col"):
            align = c.get("align", "left")
            if align not in TABLE_ALIGNMENTS:
                raise LoaderError(
                    f"{path}: <col align={align!r}> must be one of "
                    f"{sorted(TABLE_ALIGNMENTS)} (object {obj_id})"
                )
            columns.append(TableColumn(align=align))

    rows: list[TableRow] = []
    for r in elem.findall("row"):
        header = r.get("header", "false").lower() == "true"
        cells = tuple(
            TableCell(
                text=_text(c),
                colspan=_parse_span(c.get("colspan"), "colspan", path, obj_id),
                rowspan=_parse_span(c.get("rowspan"), "rowspan", path, obj_id),
            )
            for c in r.findall("cell")
        )
        rows.append(TableRow(cells=cells, header=header))

    table = Table(columns=tuple(columns), rows=tuple(rows))
    problems = table_geometry_errors(table)
    if problems:
        raise LoaderError(
            f"{path}: inconsistent table geometry (object {obj_id}): "
            + "; ".join(problems)
        )
    return table


_RULE_STATUSES = frozenset({"required", "optional", "forbidden"})
_LINK_STATUSES = frozenset({"required"})


def _parse_rules_file(path: Path) -> tuple[Rule, ...]:
    if not path.is_file():
        return ()
    root = _parse_xml(path)
    if root.tag != "rules":
        raise LoaderError(f"{path}: root must be <rules>, got <{root.tag}>")
    out: list[Rule] = []
    for rule_el in root.findall("rule"):
        when = rule_el.find("when")
        conditions: dict[str, str] = {}
        if when is not None:
            for k, v in when.attrib.items():
                conditions[k] = v
        directives: dict[str, str] = {}
        link_directives: list[LinkDirective] = []
        then = rule_el.find("then")
        if then is not None:
            for attr_el in then.findall("attr"):
                name = attr_el.get("name")
                status = attr_el.get("status")
                if not name or not status:
                    raise LoaderError(
                        f"{path}: <attr> in <then> requires @name and @status"
                    )
                if status not in _RULE_STATUSES:
                    raise LoaderError(
                        f"{path}: <attr name={name!r}> has invalid status={status!r} "
                        f"(expected one of {sorted(_RULE_STATUSES)})"
                    )
                directives[name] = status
            for link_el in then.findall("link"):
                target_doc = link_el.get("target-doc")
                status = link_el.get("status", "required")
                if not target_doc:
                    raise LoaderError(
                        f"{path}: <link> in <then> requires @target-doc"
                    )
                if status not in _LINK_STATUSES:
                    raise LoaderError(
                        f"{path}: <link target-doc={target_doc!r}> has invalid "
                        f"status={status!r} (expected one of {sorted(_LINK_STATUSES)})"
                    )
                link_directives.append(
                    LinkDirective(target_doc=target_doc, status=status)
                )
        out.append(
            Rule(
                conditions=conditions,
                directives=directives,
                link_directives=tuple(link_directives),
            )
        )
    return tuple(out)


def _parse_view(path: Path) -> View:
    root = _parse_xml(path)
    if root.tag != "view":
        raise LoaderError(f"{path}: root must be <view>, got <{root.tag}>")
    name = root.get("name")
    if not name:
        raise LoaderError(f"{path}: <view> missing required @name")
    visible: list[str] = []
    seen: set[str] = set()
    for el in root.findall("attribute"):
        attr_name = el.get("name")
        if not attr_name:
            raise LoaderError(f"{path}: <attribute> missing required @name")
        if attr_name in seen:
            raise LoaderError(f"{path}: duplicate <attribute name={attr_name!r}>")
        seen.add(attr_name)
        visible.append(attr_name)
    return View(slug=path.stem, name=name, visible=tuple(visible))


def _parse_filter(path: Path) -> Filter:
    root = _parse_xml(path)
    if root.tag != "filter":
        raise LoaderError(f"{path}: root must be <filter>, got <{root.tag}>")
    name = root.get("name")
    if not name:
        raise LoaderError(f"{path}: <filter> missing required @name")
    combinator = root.get("combinator", "all")
    if combinator not in COMBINATORS:
        raise LoaderError(
            f"{path}: <filter> combinator must be one of "
            f"{COMBINATORS}, got {combinator!r}"
        )
    keep_ancestors_attr = (root.get("keep-ancestors") or "true").lower()
    if keep_ancestors_attr not in ("true", "false"):
        raise LoaderError(
            f"{path}: <filter> keep-ancestors must be 'true' or 'false', "
            f"got {keep_ancestors_attr!r}"
        )
    keep_ancestors = keep_ancestors_attr == "true"

    rules: list[FilterRule] = []
    for el in root.findall("rule"):
        kind = el.get("kind")
        if kind not in KINDS:
            raise LoaderError(
                f"{path}: <rule> kind must be one of {KINDS}, got {kind!r}"
            )
        op = el.get("op") or ""
        attr = el.get("attr") or ""
        value = el.get("value") or ""
        if kind == "attr":
            if not attr:
                raise LoaderError(f"{path}: <rule kind='attr'> missing @attr")
            # Op validity is kind-specific; fully validating it here would
            # require knowing the attribute's schema kind, which isn't loaded
            # yet at this point. The engine validates per-evaluate. Catch the
            # obvious top-level typos by checking against the union of known
            # operator codes.
            if op and op not in _ALL_ATTR_OPS:
                raise LoaderError(
                    f"{path}: <rule kind='attr'> op={op!r} is not a recognised "
                    f"operator"
                )
        else:
            if op not in LINK_OPS:
                raise LoaderError(
                    f"{path}: <rule kind='{kind}'> op must be one of "
                    f"{LINK_OPS}, got {op!r}"
                )
        rules.append(FilterRule(kind=kind, attr=attr, op=op, value=value))

    return Filter(
        slug=path.stem,
        name=name,
        combinator=combinator,
        keep_ancestors=keep_ancestors,
        rules=tuple(rules),
    )


_ALL_ATTR_OPS: frozenset[str] = frozenset(
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


def _parse_links(path: Path) -> tuple[str, tuple[Link, ...]]:
    root = _parse_xml(path)
    if root.tag != "links":
        raise LoaderError(f"{path}: root must be <links>, got <{root.tag}>")
    source = root.get("source")
    if not source:
        raise LoaderError(f"{path}: <links> missing required @source")
    if path.stem != source:
        raise LoaderError(
            f"{path}: filename stem {path.stem!r} does not match @source={source!r}"
        )

    out: list[Link] = []
    for el in root.findall("link"):
        target_doc = el.get("target-doc")
        target_id = el.get("target-id")
        link_type = el.get("type")
        ack = el.get("ack")
        if not target_doc or not target_id or not link_type:
            raise LoaderError(
                f"{path}: <link> requires @target-doc, @target-id, @type"
            )
        out.append(
            Link(
                target_doc=target_doc,
                target_id=target_id,
                type=link_type,
                ack=ack,
            )
        )
    return source, tuple(out)

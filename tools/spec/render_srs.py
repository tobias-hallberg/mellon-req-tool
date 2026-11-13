"""Render Mellon's own requirement documents to Markdown, one file each, in outline order.

A review agent reads one Markdown file per area instead of hundreds of object files. Each
requirement shows its ID, type, status, text, rationale and table, if it has one.

Usage, from the repository root:

    python tools/spec/render_srs.py OUT_DIR [DOC_ID ...]

With no DOC_ID, every MELLON-*-SRS folder is rendered. Reads the XML directly, so it needs
no Mellon build, and changes nothing in the repository.
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def table_lines(table):
    rows = [["".join(c.itertext()).strip().replace("|", "\\|") for c in row.findall("cell")]
            for row in table.findall("row")]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    out = ["", "  | " + " | ".join(rows[0] + [""] * (width - len(rows[0]))) + " |",
           "  |" + " --- |" * width]
    out += ["  | " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows[1:]]
    return out + [""]


def render(doc: Path) -> tuple[str, int, int]:
    objs = {}
    for f in (doc / "objects").glob("*.xml"):
        e = ET.parse(f).getroot()
        attrs = {a.get("name"): (a.text or "") for a in e.findall("attr")}
        desc = e.find("description")
        objs[e.get("id")] = (attrs, (desc.text or "") if desc is not None else "", e.find("table"))
    outline = ET.parse(doc / f"{doc.name}.xml").getroot().find("outline")
    lines = [f"# {doc.name}", ""]

    def walk(node, depth):
        for ref in node.findall("ref"):
            oid = ref.get("id")
            attrs, text, table = objs[oid]
            if attrs.get("type") == "Heading":
                lines.extend(["", "#" * min(depth + 1, 6) + f" {text} ({oid})", ""])
            else:
                lines.append(f"- **{oid}** ({attrs.get('req-type', '?')}, {attrs.get('status', '?')}): {text}")
                if attrs.get("rationale"):
                    lines.append(f"  - Rationale: {attrs['rationale']}")
                if table is not None:
                    lines.extend(table_lines(table))
            walk(ref, depth + 1)

    walk(outline, 1)
    built = sum(1 for a, _, _ in objs.values() if a.get("status") in ("Implemented", "Accepted"))
    return "\n".join(lines) + "\n", len(objs), built


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    docs = [ROOT / d for d in sys.argv[2:]] or sorted(ROOT.glob("MELLON-*-SRS"))
    for doc in docs:
        text, count, built = render(doc)
        (out_dir / f"{doc.name}.md").write_text(text, encoding="utf-8")
        print(f"{doc.name}: {count} objects, {built} built")


if __name__ == "__main__":
    main()

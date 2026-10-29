"""Suspect-link detection + acknowledgment.

Suspect-link algorithm (see PLAN.md §4):
    For each outgoing <link>:
        1. Resolve target file: <repo_root>/<target-doc>/objects/<target-id>.xml
        2. If missing -> BROKEN
        3. Get target's last-commit short hash from git (object XML + figure
           SVG, following git's history simplification across merges)
        4. If the link has no @ack, or @ack != current_hash -> SUSPECT
        5. Otherwise -> OK
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from lxml import etree

from .git_ops import discover_repo, last_commit_for_paths
from .schema import Document, Link


class LinkState(str, Enum):
    OK = "ok"
    SUSPECT = "suspect"
    BROKEN = "broken"


@dataclass(frozen=True, slots=True)
class LinkStatus:
    source_id: str
    link: Link
    state: LinkState
    current_hash: str | None  # current last-commit short hash of target, if any
    reason: str  # short human-readable explanation


def scan_document(doc: Document, doc_folder: Path) -> list[LinkStatus]:
    """Classify every outgoing link in `doc`.

    `doc_folder` is the on-disk path of the document whose links we're scanning.
    The enclosing git repo is discovered automatically.
    """
    repo, repo_root = discover_repo(doc_folder)
    results: list[LinkStatus] = []
    # Many links share a target; each target's history is walked once.
    current_by_target: dict[tuple[str, str], str | None] = {}
    for source_id, link_list in doc.links.items():
        for link in link_list:
            obj_rel = f"{link.target_doc}/objects/{link.target_id}.xml"
            fig_rel = f"{link.target_doc}/figures/{link.target_id}.svg"
            if not (repo_root / obj_rel).is_file():
                results.append(
                    LinkStatus(source_id, link, LinkState.BROKEN, None, "target file missing")
                )
                continue

            key = (link.target_doc, link.target_id)
            if key not in current_by_target:
                current_by_target[key] = last_commit_for_paths(repo, [obj_rel, fig_rel])
            current = current_by_target[key]
            if current is None:
                results.append(
                    LinkStatus(source_id, link, LinkState.BROKEN, None, "target not in git history")
                )
                continue

            if link.ack is None:
                results.append(
                    LinkStatus(source_id, link, LinkState.SUSPECT, current, "never reviewed")
                )
            elif link.ack != current:
                results.append(
                    LinkStatus(
                        source_id, link, LinkState.SUSPECT, current,
                        f"ack={link.ack} but target is at {current}",
                    )
                )
            else:
                results.append(LinkStatus(source_id, link, LinkState.OK, current, "ok"))
    return results


def scan_repo(repo_root: Path) -> dict[str, list[LinkStatus]]:
    """Scan every document under `repo_root` for suspect links.

    Returns a mapping of doc_id -> link statuses for that doc. Documents with
    no outgoing links appear with an empty list.
    """
    from .repo import load_all

    out: dict[str, list[LinkStatus]] = {}
    for doc_id, loaded in load_all(repo_root).items():
        out[doc_id] = scan_document(loaded.document, loaded.folder)
    return out


def write_ack(
    links_file: Path, target_doc: str, target_id: str, new_ack: str
) -> bool:
    """Rewrite @ack on the matching <link> in `links_file`.

    Returns True if a link was updated. Matching is by target-doc + target-id;
    if the same target appears multiple times, all matches are updated.
    """
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.parse(str(links_file), parser)
    root = tree.getroot()

    changed = False
    for link in root.findall("link"):
        if (
            link.get("target-doc") == target_doc
            and link.get("target-id") == target_id
        ):
            if link.get("ack") != new_ack:
                link.set("ack", new_ack)
                changed = True

    if changed:
        from .editor import _write_tree

        _write_tree(tree, links_file)
    return changed


def ack_all(
    doc_folder: Path, statuses: list[LinkStatus]
) -> list[LinkStatus]:
    """Update @ack for every SUSPECT link in `statuses` to its current target hash.

    Returns the list of statuses that were actually rewritten on disk.
    BROKEN links are skipped (no current hash to write). All or nothing: if
    one file can't be written, the ones already rewritten are put back.
    """
    from .editor import journal

    rewritten: list[LinkStatus] = []
    with journal():
        for s in statuses:
            if s.state is not LinkState.SUSPECT or s.current_hash is None:
                continue
            links_file = doc_folder / "links" / f"{s.source_id}.xml"
            if write_ack(links_file, s.link.target_doc, s.link.target_id, s.current_hash):
                rewritten.append(s)
    return rewritten

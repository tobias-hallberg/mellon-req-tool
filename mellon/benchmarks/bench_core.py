"""Library and MCP timings on a repo from gen_repo.py. Usage: bench_core.py REPO

Starts by deleting the link index, so the first scan is a first-ever one. The
repo is edited and committed to, so generate a fresh one for each run.
"""

import json
import sys
import time
from pathlib import Path

import pygit2

from mellon import editor, git_ops, load_document
from mellon.diff import WORKING_COPY, compare_document, document_versions
from mellon.filter import evaluate
from mellon.mcp import server
from mellon.repo import folders_fingerprint, load_all
from mellon.schema import Filter, FilterRule
from mellon.service.document import DocumentService
from mellon.suspects import LinkState, scan_document

ROOT = Path(sys.argv[1]).resolve()
results: dict[str, float | int | str] = {}
(ROOT / ".git" / "mellon-link-index.json").unlink(missing_ok=True)


def t(label, fn, repeat=1):
    best = None
    out = None
    for _ in range(repeat):
        start = time.perf_counter()
        out = fn()
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    results[label] = round(best, 4)
    print(f"{label:45s} {best * 1000:12.1f} ms", flush=True)
    return out


repo = pygit2.Repository(str(ROOT))
t("load_all, cold files", lambda: load_all(ROOT))
doc = t("load_document(SRS)", lambda: load_document(ROOT / "SRS"), repeat=2)
docs = t("load_all (both docs)", lambda: load_all(ROOT), repeat=2)
svc = DocumentService(ROOT, cache=True)
t("validate_repo", lambda: svc.validate_repo())
statuses = t("scan_document(SRS), first ever", lambda: scan_document(doc, ROOT / "SRS"))
assert all(s.state is LinkState.OK for s in statuses), "generator acks are wrong"
results["links"] = len(statuses)
t("scan_document(SRS), same HEAD", lambda: scan_document(doc, ROOT / "SRS"))
t("repo.status()", lambda: repo.status())
t("dirty_paths_under(SRS)", lambda: git_ops.dirty_paths_under(repo, "SRS"))
t("folders_fingerprint(SRS,SYS)", lambda: folders_fingerprint([ROOT / "SRS", ROOT / "SYS"]))

# Compare (MELLON-SRS: within 2 s at 1,000 objects). From the first commit to
# HEAD changes the most objects; the working copy reads every file.
first_commit = str(list(repo.walk(repo.head.target, pygit2.GIT_SORT_TOPOLOGICAL))[-1].id)
diff = t("compare_document(SRS), first..HEAD", lambda: compare_document(ROOT, "SRS", first_commit, "HEAD"))
results["compare changes"] = len(diff.changes)
t("compare_document(SRS), HEAD..working copy", lambda: compare_document(ROOT, "SRS", "HEAD", WORKING_COPY))
t("document_versions(SRS)", lambda: document_versions(ROOT, "SRS"))

# Incoming-link filter: the provider scans every link in the repo per object.
rule_all = Filter(slug="x", name="x", rules=(FilterRule(kind="has-incoming", attr="", op="present", value=""),))
sys_doc = docs["SYS"].document
provider = lambda target: [d for d, _s, _l in svc.incoming_links("SYS", target)]
t("filter has-incoming on SYS", lambda: evaluate(sys_doc, rule_all, incoming_provider=provider))

# MCP tools, each with the uncached service they really run on.
server._State.repo_root = ROOT


def mcp(label, fn):
    out = t(f"mcp {label}", fn)
    size = len(json.dumps(out, default=str))
    results[f"mcp {label} bytes"] = size
    print(f"{'':45s} {size:12,d} bytes", flush=True)


mcp("list_documents", server.list_documents)
mcp("show_document", lambda: server.show_document("SRS"))
mcp("show_object", lambda: server.show_object("SRS", "SRS-2"))
mcp("inspect_object", lambda: server.inspect_object("SRS", "SRS-2"))
mcp("outline", lambda: server.outline("SRS"))
mcp("search_objects", lambda: server.search_objects("latch"))
mcp("suspects(SRS)", lambda: server.suspects("SRS"))
mcp("trace", lambda: server.trace("SRS", "SRS-2"))
mcp("coverage_report", lambda: server.coverage_report("SYS", "SRS"))
mcp("repo_health", server.repo_health)
mcp("set_attribute", lambda: server.set_attribute("SRS", "SRS-2", "status", "Reviewed"))
mcp("new_object", lambda: server.new_object("SRS", description="benchmark object"))
mcp("add_link", lambda: server.add_link("SRS", "SRS-1", "SYS", "SYS-1", "satisfies"))
mcp("commit_changes", lambda: server.commit_changes("bench commit"))
t("scan_document(SRS), after one commit", lambda: scan_document(load_document(ROOT / "SRS"), ROOT / "SRS"))
t("editor.set_attribute alone", lambda: editor.set_attribute(ROOT / "SRS", "SRS-3", "status", "Reviewed"))
print(json.dumps(results))

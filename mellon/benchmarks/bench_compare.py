"""Time MELLON-VER-SRS-57's comparison on a repo from gen_repo.py.

Usage: bench_compare.py REPO

Changes every SRS object's description and one attribute, commits, and times
comparing that commit with the one before, best of 5. The repo is committed
to, so generate a fresh one for each run.
"""

import json
import sys
import time
from pathlib import Path

from mellon import editor, git_ops, load_document
from mellon.diff import compare_document

ROOT = Path(sys.argv[1]).resolve()
folder = ROOT / "SRS"
doc = load_document(folder)
before = str(git_ops.discover_repo(ROOT)[0].head.target)
# One attribute per object: an enum other than `type`, set to another option.
attr = next(a for a in doc.attributes if a.kind == "enum" and a.name != "type" and len(a.options) > 1)
for obj_id, obj in doc.objects.items():
    editor.set_description(folder, obj_id, f"{obj.description} Changed.")
    other = next(o for o in attr.options if o != obj.attrs.get(attr.name))
    editor.set_attribute(folder, obj_id, attr.name, other)
git_ops.commit(ROOT, "change every object")
after = str(git_ops.discover_repo(ROOT)[0].head.target)

best = None
for _ in range(5):
    start = time.perf_counter()
    diff = compare_document(ROOT, "SRS", before, after)
    took = time.perf_counter() - start
    best = took if best is None else min(best, took)
print(json.dumps({"objects": len(doc.objects), "changes": len(diff.changes), "best_s": round(best, 3)}))

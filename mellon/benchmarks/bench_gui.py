"""GUI timings, offscreen, on a repo from gen_repo.py.

Usage: bench_gui.py REPO [PROFILE_OUT] [--real-window]

The repo is edited and committed to. --real-window uses the desktop's Qt
platform instead of offscreen, so painting is real and a window shows.
"""

import cProfile
import json
import os
import pstats
import sys
import time
from pathlib import Path

args = [a for a in sys.argv[1:] if a != "--real-window"]
if "--real-window" not in sys.argv:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from mellon.gui.main_window import DocumentEditorWindow  # noqa: E402
from mellon.service.document import DocumentService  # noqa: E402

ROOT = Path(args[0]).resolve()
PROFILE = args[1] if len(args) > 1 else None
results: dict[str, float | int] = {}

app = QApplication([])


def t(label, fn):
    start = time.perf_counter()
    out = fn()
    app.processEvents()
    elapsed = time.perf_counter() - start
    results[label] = round(elapsed, 4)
    print(f"{label:45s} {elapsed * 1000:12.1f} ms", flush=True)
    return out


def open_window():
    svc = DocumentService(ROOT, cache=True)
    w = DocumentEditorWindow("SRS", svc)
    w._set_read_only(False)
    w.resize(1400, 900)
    w.show()
    return w


w = t("open window, first in process", open_window)
w.close()
w = t("open window, second in process", open_window)

grid = w.grid_pane
model = grid._model
table = grid.table
c = w.controller
assert model.rowCount() > 0
status_col = model.column_index("status")
results["rows"] = model.rowCount()

t("grab (first full paint)", w.grab)
t("resizeRowsToContents, all rows", table.resizeRowsToContents)
t("resizeRowToContents, one row", lambda: table.resizeRowToContents(5))
t("select another object", lambda: c.select_object("SRS", "SRS-200"))
t("select back", lambda: c.select_object("SRS", "SRS-3"))
t("build_link_states, cached scans", lambda: c.build_link_states("SRS"))
t("build_dirty_ids", lambda: c.build_dirty_ids("SRS"))
t("disk freshness check", c.changed_figures)


def edit(row: int, value: str):
    assert model.setData(model.index(row, status_col), value)


t("cell edit: first", lambda: edit(10, "Approved"))
t("cell edit: second", lambda: edit(11, "Approved"))

if PROFILE:
    prof = cProfile.Profile()
    prof.enable()
    edit(12, "Approved")
    app.processEvents()
    prof.disable()
    with open(PROFILE, "w") as f:
        stats = pstats.Stats(prof, stream=f)
        stats.sort_stats("cumulative").print_stats(45)
        stats.sort_stats("tottime").print_stats(25)

t("filter box: type text", lambda: grid.filter_box.setText("latch"))
t("filter box: clear", lambda: grid.filter_box.setText(""))
t("suspect-only on", lambda: grid.set_suspect_only(True))
t("suspect-only off", lambda: grid.set_suspect_only(False))
t("F5 reload", lambda: c.reload_document("SRS"))
t("new object (structural edit)", lambda: (c.service.create_object("SRS", description="benchmark"), c.reload_document("SRS")))
t("prepare_document_commit", lambda: c.prepare_document_commit("SRS"))
t("commit document", lambda: c.commit_document("SRS", "bench commit"))
t("link states, cached", lambda: c.build_link_states("SRS"))
c.service.cache.invalidate_suspects()
t("link states, re-read after one commit", lambda: c.build_link_states("SRS"))
print(json.dumps(results))

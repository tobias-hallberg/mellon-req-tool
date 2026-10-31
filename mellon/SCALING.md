# Scaling

How Mellon behaves as documents grow, why, and what is left to do. The numbers come from `benchmarks/`, so they can be re-measured.

## Method

`benchmarks/gen_repo.py` builds a repo with two linked documents of N objects each. Every non-heading object in the child links to one object in the parent, and all links start OK. History is N commits, each touching 1–3 random objects, so the repo looks like a document that has been edited about once per object.

```
uv run --no-sync python benchmarks/run_all.py <out-dir> [--export] [N ...]
```

Limits of these numbers:

- The data is synthetic, and the history is linear.
- Qt runs offscreen, so real painting isn't included unless `bench_gui.py` is given `--real-window`.
- Timings are from one Windows machine with antivirus scanning on.

## Before this work

Measured on v0.3.0 plus the suspect-link hardening.

| Default behaviour | 500 | 2,000 | 5,000 |
|---|---|---|---|
| Open window | 1.1 s | 6.6 s | not open after 30 min |
| Edit one cell | 1.4 s | 11 s | not reached |
| Suspect scan, fresh process | 0.28 s | 4.9 s | about 87 min (extrapolated) |
| Rescan later in a GUI session | 0.48 s | 222 s | not reached |

With one real scan and its result reused, to expose the other costs. Other benchmarks were running, so these may read up to about 2× high:

| Scan result reused | 500 | 2,000 | 5,000 |
|---|---|---|---|
| Edit one cell | 1.5 s | 6.9 s | 23 s |
| Select an object | 0.05 s | 0.8 s | 4.7 s |
| F5 reload | 0.9 s | 3.8 s | 12.5 s |
| Suspect-only toggle | 0.2 s | 1.0 s | 3.4 s |
| MCP `set_attribute` | 0.3 s | 0.9 s | 4.9 s |

These stayed fast at 5,000: `repo.status()` 52 ms, painting 25 ms, the text filter 116 ms, and re-measuring one grid row under 1 ms.

## Why it was slow

**The suspect scan.** It walked history separately for every link target, so its cost grew with targets × commits. Each step also read the target document's `objects` tree, and that tree grows with the document. At 5,000 objects this filled libgit2's process-wide 256 MB object cache during the first scan, and each target then cost about 1 s instead of a few milliseconds. The 222 s rescan at 2,000 fits the same cause, though the cliff itself was only reproduced in a separate test.

**The edit path.** With the scan out of the way, a 23 s edit at 5,000 broke down as:

- 34% refreshing the History tab
- 31% parsing the whole document three times
- 18% fingerprinting files on disk
- 16% resetting and re-measuring the grid

## Remaining work

Costs are at 5,000 objects.

**Reverse-link index.** Incoming links are found by scanning every link in the repo:

- has-incoming filter: 5.1 s
- `trace`: 10 s
- `build_link_states`: 0.35 s

**MCP.** The server runs uncached, so documents are re-read on every call. The loader's parse cache should remove most of that cost, so re-measure before designing anything. There is no pagination, and `outline` returns 1 MB of JSON, which is too much for an agent to use.

**PDF export.** 58–126 s at 500 objects. At 5,000 it hits tectonic's fixed 300 s timeout, whose error message wrongly suggests a network problem. Whether it finishes at all is unknown.

**History tab while visible.** Selecting an object walks history until it finds 10 changes, about 4.8 s when the object rarely changes. The tab only loads while it is visible, so this costs nothing otherwise.

**Structural edits.** Create, delete and move still reset the grid model. Move to row inserts and removes if a reset turns out too slow.

**Batch editor primitives.** Each editor call loads the document and writes its files on its own, so a bulk operation repeats that work.

**Memory.** Readings across runs were inconsistent, so memory use at 5,000 is not established.

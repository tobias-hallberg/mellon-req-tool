# Scaling

How Mellon behaves as documents grow, why, and what is left to do. The numbers come from `benchmarks/`, so they can be re-measured.

## Recommended size

Keep a document to 1,000 objects or fewer, counting headings and information text. A larger specification is better split into components, each with its own set of documents. Nothing enforces this.

- **PDF export sets the limit.** A document has to be issued, and export took 101 s at 1,000 objects and 191 s at 2,000, against tectonic's fixed 300 s timeout. At 5,000 it hits the timeout. 1,000 leaves room for a slower machine or a busy one.
- **Everything else is comfortable at 1,000.** Field edits and link checks stay fast up to 5,000. Opening, reloading and structural edits take several seconds at 5,000; see the tables below.
- **Beyond the measurements:** a document much larger than this is hard to review as one piece, and it usually covers several components anyway.

Splitting has one constraint: an object can only link within its own document or to a declared parent, so requirements that components share belong in a common parent document. Repos with many documents haven't been benchmarked; the has-incoming filter scans every link in the repo, so splitting doesn't make it faster.

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

## After this work

Old and new code ran side by side on identical repos, so they saw the same load. The machine was about 60–65% busy with unrelated work throughout, so all of these read high.

| Offscreen, old → new | 500 | 2,000 |
|---|---|---|
| Open window, second in process | 3.2 s → 0.8 s | 25 s → 2.6 s |
| Edit one cell | 4.3 s → 0.11 s | 42 s → 0.23–0.34 s |
| Select an object, History hidden | 0.13 s → 0.03 s | 1.1 s → 0.04 s |
| F5 reload | 2.3 s → 0.5 s | 21 s → 1.7 s |
| New object | 2.8 s → 0.5 s | 24 s → 1.9 s |
| Suspect-only toggle | 0.26 s → 0.01 s | 1.4 s → 0.04 s |
| Link states after one commit | 1.1 s → 0.02 s | not reached → 0.03 s |

The old code at 2,000 didn't finish its commit step within 280 s.

| At 5,000 objects, new code | Offscreen | Real window | Target | Met? |
|---|---|---|---|---|
| Suspect scan, first ever | 13 s (4.0 s on a quieter run) | 11 s | ≤ 10 s | not under load |
| Link states after one commit | 0.05 s | 0.15 s | ≤ 0.2 s | yes |
| Open window, index on disk | 5.3 s | 6.4 s | ≤ 5 s | no |
| Edit one cell | 0.49 s | 0.41 s | ≤ 0.5 s | yes |
| Select an object, History hidden | 0.04 s | 0.07 s | ≤ 0.2 s | yes |
| F5 reload | 3.5 s | 4.3 s | ≤ 3 s | no |
| New object | 3.9 s | 4.6 s | ≤ 3 s | no |
| Suspect-only toggle | 0.09 s | 0.09 s | ≤ 0.2 s | yes |

The targets were estimates set before the work. The open and reload times are mostly Qt measuring every cell; see below.

**First read of new files.** The very first open after the files were written took 21 s at 500 objects, about 80–97 s at 2,000 and 81 s at 5,000, on old and new code alike. A second open is far faster, so this is the cost of reading files for the first time, which on this machine looks like antivirus scanning. That wasn't verified. Expect it after a fresh clone, not in daily use.

## Why it was slow

**The suspect scan.** It walked history separately for every link target, so its cost grew with targets × commits. Each step also read the target document's `objects` tree, and that tree grows with the document. At 5,000 objects this filled libgit2's process-wide 256 MB object cache during the first scan, and each target then cost about 1 s instead of a few milliseconds. The 222 s rescan at 2,000 fits the same cause, though the cliff itself was only reproduced in a separate test.

**The edit path.** With the scan out of the way, a 23 s edit at 5,000 broke down as:

- 34% refreshing the History tab
- 31% parsing the whole document three times
- 18% fingerprinting files on disk
- 16% resetting and re-measuring the grid

## Remaining work

Costs are at 5,000 objects.

**Model resets.** Create, delete, move, undo and F5 still reset the grid model, 3.5–4.6 s under load. Most of that is Qt measuring every cell of every row, about 295,000 Python `data()` calls. The text-height cache already removed the description layout. Either measure rows in one pass of our own, or move structural edits to row inserts and removes.

**Reverse-link index.** The has-incoming filter scans every link in the repo for each object: 5–14 s depending on load. An active filter like that also re-runs after each field edit, which would undo the fast edit path.

**History walks.** `log_for_path` walks history until it finds 10 changes. With the History tab visible, selecting an object costs about 4.8 s when the object rarely changes (measured before this work). MCP `inspect_object` with history took 15 s under load. `link_index` shows how to avoid reading the large tree for every commit.

**MCP.** The parse cache removed most of the cost of re-reading documents: `set_attribute` 4.9 s → 0.2 s, `trace` 10 s → 0.4 s, `show_object` 8.9 s → 0.4 s. The whole `outline` is 1 MB of JSON, too much for an agent to use, so it now takes `root` and `depth` and an agent can read one section at a time. The other reads have no pagination.

**PDF export.** 58–126 s at 500 objects with the machine busy. On a mostly idle machine, with the installer's tectonic and TeX cache, it took 101 s at 1,000 and 191 s at 2,000, so it grows about linearly. At 5,000 it hits tectonic's fixed 300 s timeout, whose error message wrongly suggests a network problem. Whether it finishes at all without the timeout is unknown. This work didn't change it.

**Batch editor primitives.** Each editor call loads the document and writes its files on its own, so a bulk operation repeats that work.

**Memory.** Readings across runs were inconsistent, so memory use at 5,000 is not established.

# Failure analysis

What happens when Mellon's functions fail, and which requirements must prevent or handle each result. It is checked against [architecture.md](architecture.md) and follows the engineering framework's architecture checklist (items H1 and H3 here; the per-component F-items in each area's section).

Mellon has no vehicle and no people around it, so the effects are rated on four levels instead, worst first:

| Effect | Meaning |
|---|---|
| **Data loss** | Stored requirements, links, history or tags are lost, damaged or changed without the user asking. Recoverable from git counts as loss of the uncommitted work, or of the user's trust in what is stored. |
| **Wrong answer** | Mellon reports a state that isn't true (valid, OK, covered, unchanged, issued, complete), and nothing on screen says so. Often worse than data loss, because nobody goes looking. |
| **Degraded** | A function is refused, fails or is slow, and the user can tell. |
| **Nuisance** | Extra work or noise. Nothing wrong is stored or believed. |

Mellon is `High` throughout ([architecture.md](architecture.md#level-h4)), so every data loss and wrong answer below needs a requirement (H3).

**Status.** This first pass covers the whole system (H1, H3). It was written from the code's structure, without requirements for most areas. The analysis per component and interface is added one area at a time, in the sections at the end, as each area is specified ([specification.md](specification.md#working-on-the-specification), step 4).

## Effects of each function failing (H1)

Failure kinds: **lost** (doesn't happen), **unwanted** (happens when it shouldn't), **too much** or **too little**, and **wrong** (an answer that looks right). Only the kinds that matter for a function are listed. "By design" marks a behaviour that is intended today but still needs a requirement that says so.

### Storing and loading

| ID | Function | Failure | What happens | Effect |
|---|---|---|---|---|
| E1 | Save | Lost: a file stays locked past the retries, the disk is full, permission denied | The operation is refused with an error and the files it touched are put back. | Degraded |
| E2 | Save | Too little: the process is killed or power fails during an operation across several files | Files written so far stay, because the rollback journal is in memory. A delete can strip the incoming links in other documents and stop before removing the object. Nothing reports it. | Data loss |
| E3 | Save | Wrong: power fails after the replace, before the data is on disk (no flush) | The file may be empty or partly written (`TBD`). The document fails to load; uncommitted edits to it are gone. | Data loss |
| E4 | Save | Unwanted: an edit is written over a change another writer made (MCP, a pull, another window) while this window stayed active | The other change to that field is overwritten without a warning. | Data loss |
| E5 | Save | Too much: two processes rewrite the same manifest at once | One outline change is lost. A lost ref shows as `OBJECT_NOT_IN_OUTLINE`; a lost move doesn't show. | Data loss |
| E6 | Save | Too much: the first edit of a hand-written file re-indents it | One-time diff noise. Links to that object go SUSPECT with no field changed. | Nuisance |
| E7 | Save | Unwanted: a crash leaves a `.<name>.<pid>.tmp` file | It counts as an uncommitted change and goes into the next commit of that document. | Nuisance |
| E8 | Text cleanup | Wrong: vertical tab and form feed become newlines, CR becomes LF | Stored text differs from what was pasted; page breaks are lost. Other control characters are refused. By design. | Nuisance |
| E9 | Load | Lost: a file doesn't parse or breaks a naming rule | The document is listed as failing, with file and line. Delete and the repo-wide suspect scan are refused. | Degraded |
| E10 | Load | Too little: a failing document is left out of trace, incoming links, coverage, search, has-incoming filters and the GUI's incoming link states | The answers look complete and aren't. Over MCP, only `list_documents`, `validate_repo`, `repo_health` and `suspects()` say a document failed. | Wrong answer |
| E11 | Load | Wrong: the GUI keeps documents a window doesn't watch (children, siblings) cached until F5 | After an MCP or git-client change, incoming links, trace, coverage, filters and BROKEN states show the old state. | Wrong answer |
| E12 | Load | Wrong: the parse cache keeps an old parse of a file rewritten with the same size and time | The old content is shown, then written over. Needs a clock or file-system oddity: files younger than 2 s aren't kept. | Wrong answer |
| E13 | Discovery | Too little: a document folder nested below the root, or starting with a dot | Not listed and not in any repo-wide answer, with no error. By design. | Wrong answer |
| E14 | Repository root | Unwanted: the MCP server serves another full clone than meant (`CLAUDE_PROJECT_DIR` or the current directory) | Reads and writes go to that repository. Only the startup log names it. | Data loss |
| E15 | Repository root | Lost: shallow clone, subfolder, not owned | Refused with the reason. | Degraded |

### Editing

| ID | Function | Failure | What happens | Effect |
|---|---|---|---|---|
| E16 | Create object | Wrong: deleting the highest-numbered object frees its number, and the next new object takes the same id (checked: `D-1` deleted, the next object is `D-1`) | Links on other branches, History, Compare, issued PDFs and test reports name the old requirement by an id that now means another. Compare shows Modified, not Deleted and Added. | Wrong answer |
| E17 | Create object | Too much: two processes create an object at the same moment | Both take the same id; one file replaces the other; the outline gets two refs (`OUTLINE_DUP`). | Data loss |
| E18 | Edit field | Unwanted: an edit lands on another object than meant (selection changed under pending text) | Wrong text on a requirement. GUI undo until commit. | Data loss |
| E19 | Schema change | Too much: deleting an attribute or an option, or defining one with a default, rewrites every object that has it | Values change across the document in one call, and every link to those objects goes SUSPECT. No undo over MCP. | Data loss |
| E20 | Delete | Lost: refused (sub-objects, a document fails to load) | Message. | Degraded |
| E21 | Delete | Unwanted: the wrong object | Object, figure, outgoing links and other documents' incoming links removed. GUI undo until commit; none over MCP; the committed copy is in git. | Data loss |
| E22 | Delete | Too little: links to the object made on another branch | They read BROKEN after the merge. | Degraded |
| E23 | Delete | Too much: other documents' links are stripped, and the GUI commits one document | The other documents stay uncommitted (TEAM.md step 5). | Nuisance |
| E24 | Undo | Unwanted: undo would write over another writer's change | Refused, because each file must still hold the step's after-bytes. | Degraded |
| E25 | Undo | Lost: crash, commit, F5, Discard, the stale banner | History gone. | Degraded |
| E26 | Filter | Wrong: the active filter is remembered per document id, so it follows to another repository's document of the same id | Rows hidden; the pill shows the filter. | Nuisance |

### Links and checks

| ID | Function | Failure | What happens | Effect |
|---|---|---|---|---|
| E27 | Add link | Wrong: a new link takes the target's latest commit as its ack | The link reads OK at once: creating it counts as reviewing the target. If the target had uncommitted edits, the user saw text the ack doesn't cover. By design. | Wrong answer |
| E28 | Suspect scan | Lost: the scan fails (git or load error) | The GUI shows no suspect banner, an OK gutter and no export warning. MCP returns an error. | Wrong answer |
| E29 | Suspect scan | Wrong: a link reads OK though its target changed: a wrong entry in `.git/mellon-link-index.json` is trusted; two commits share the 7-character short hash; the change was to the outline, the attribute definitions or the target's heading, not its object file or figure | No review is asked for. | Wrong answer |
| E30 | Suspect scan | Wrong: the target changed only in the working copy | Reads OK until the change is committed. By design (`parent_uncommitted`). | Wrong answer |
| E31 | Suspect scan | Too much: SUSPECT with nothing to review (rebase, squash, cherry-pick, a formatting-only rewrite, a renumbered target) | Review churn; reviewers learn to ack without reading. | Nuisance |
| E32 | Acknowledge | Too much: MCP `ack_suspects` without `link_keys` acks every suspect link in the document | Links read OK that nobody reviewed. | Wrong answer |
| E33 | Acknowledge | Wrong: the ack records the target's latest commit, not the commit reviewed | A commit that landed between the review and the ack is accepted unseen. | Wrong answer |
| E34 | Acknowledge | Lost: the ack fails | The link stays SUSPECT. | Degraded |
| E35 | Review suspect link | Wrong: `changed_fields` is empty for a formatting-only change, or null when the ack can't be resolved | An agent reads "nothing changed" and acks. | Wrong answer |
| E36 | Validation | Wrong: the GUI validates its cached copy, and the commit takes the files on disk | An invalid document is committed after "validation passed". | Wrong answer |
| E37 | Validation | Lost: MCP commits don't validate | Invalid documents are committed; `mellon-check` in CI catches them if it runs. | Degraded |
| E38 | Validation | Too little: a link to a missing object in another document isn't a validation error | `validate_repo` says `ok`; only the suspect scan reports it as BROKEN. | Wrong answer |
| E39 | Validation | Too much: a false error | A GUI commit is blocked. | Degraded |
| E40 | `mellon-check` | Wrong: exit 0 for a root with no documents (a wrong `--repo-root` that is still a git root) | CI passes having checked nothing. Only the summary says "0 document(s)". | Wrong answer |
| E41 | `mellon-check` | Wrong: it uses the same service and suspect rule as the GUI and MCP | A bug there gives the same wrong answer in the gate. | Wrong answer |
| E42 | `mellon-check` | Lost: shallow checkout | Exit 2. | Degraded |
| E43 | Coverage | Wrong: a link counts as coverage whatever its state, SUSPECT or BROKEN | Coverage reads complete with links that don't hold. | Wrong answer |
| E44 | Trace | Too little: MCP `trace` cuts at `max_depth` (default 2) and shows cut branches as leaves | An impact analysis misses everything deeper and can't tell. | Wrong answer |

### Versions

| ID | Function | Failure | What happens | Effect |
|---|---|---|---|---|
| E45 | Commit | Lost: refused (identity, merge, `index.lock`, tip moved) | Nothing committed; message. | Degraded |
| E46 | Commit | Too much: the GUI commit takes the folder's dirty files as they are when it runs, including MCP edits made while the dialog was open and files staged by another client | The commit holds changes its author didn't see, under their name and message. | Wrong answer |
| E47 | Commit | Wrong: commit on a detached HEAD | It lands on no branch and drops out of sight at the next checkout (the reflog keeps it). | Data loss |
| E48 | Commit | Too little: one document per GUI commit | Other documents' changes stay behind. By design. | Nuisance |
| E49 | Discard | Unwanted: the wrong document, or new objects the user didn't mean to lose | Uncommitted work in the folder is gone, with no undo. Links from other documents to removed objects read BROKEN. | Data loss |
| E50 | Discard | Too little: fails partway | Running it again finishes. | Degraded |
| E51 | History | Wrong: a reused id (E16) shows the old object's commits | — | Wrong answer |
| E52 | Compare | Wrong: a reused id (E16) shows as Modified | — | Wrong answer |
| E53 | Compare | Too little: only objects are compared; metadata, parents, attribute definitions, rules, views and filters aren't listed | "No changes" between two issues whose rules or parents changed. | Wrong answer |
| E54 | Compare | Lost: an unparseable file, a version that can't be resolved | Marked unreadable, or an error naming the version. | Degraded |
| E55 | Issue tag | Lost: the tag fails after the commit | An 8 s status-bar message only. The user believes the issue was cut; the PDF and Compare use the previous issue. | Wrong answer |
| E56 | Issue tag | Wrong: the GUI tags the HEAD read after the commit; MCP `tag_issue` tags whatever HEAD is when called | If another commit landed in between, the issue marks content its author didn't commit. | Wrong answer |
| E57 | Issue tag | Too little: lightweight tags aren't pushed by a plain `git push` | Other clones show the previous issue. | Wrong answer |
| E58 | Tag | Lost: the free tag fails | Status-bar message; the commit stands. | Degraded |
| E59 | Merge: renumber | Wrong: the side that yields is chosen by commit time, from each machine's clock | A requirement already issued, or named outside the repository (PDFs, tickets, test reports), can be the one renumbered. Inside the repository its links follow it. | Wrong answer |
| E60 | Merge: renumber | Too little: a link to the clashing id made on neither side's branch (a third branch merged later) | It points at the other object, reads SUSPECT, and an ack accepts the wrong target. | Wrong answer |
| E61 | Merge: renumber | Lost: conflicts resolved in a git client instead | Duplicate ids and outline refs, links on the wrong object. Load and validation errors catch some of it. | Data loss |
| E62 | Merge: resolve | Too much: every document file both sides changed is rewritten, whatever the work tree holds; editing isn't refused during a merge | Edits made in Mellon to those files after the merge stopped are overwritten without a warning. | Data loss |
| E63 | Merge: resolve | Wrong: fields from two sides combine into an object nobody wrote (status Approved from one, new text from the other) | Links to it go SUSPECT; the object itself isn't flagged for review. | Wrong answer |
| E64 | Merge: resolve | Too little: defaults (ours, keep) accepted unread | Their change to that field is dropped. | Data loss |
| E65 | Merge: resolve | Lost: fails partway | Running it again finishes. | Degraded |

### Export, MCP and the GUI

| ID | Function | Failure | What happens | Effect |
|---|---|---|---|---|
| E66 | Export | Wrong: the PDF shows the working copy, uncommitted edits included, labelled with the current issue, with no warning | A distributed "issue N" doesn't match the tagged commit. | Wrong answer |
| E67 | Export | Wrong: MCP exports with suspect or broken links and only lists them | A PDF from a document with unreviewed links. | Wrong answer |
| E68 | Export | Too little: figures not rendered (draw.io missing, an empty diagram); a multi-page diagram exports page 1 | Placeholder and notice for the first; later pages dropped silently. | Degraded |
| E69 | Export | Lost: timeout (about 3,000 objects and up), tectonic missing, empty cache offline | Error; the timeout message wrongly suggests the network. | Degraded |
| E70 | Export | Too little: crash during the write | A truncated PDF at the target. | Nuisance |
| E71 | MCP write | Unwanted: an agent writes what the user didn't ask | No undo over MCP; with `commit_message` it is committed at once. Git keeps the history. | Data loss |
| E72 | MCP write | Wrong: `affected_files` misses a file the call changed | Auto-commit leaves part of the operation uncommitted. Not checked. | Wrong answer |
| E73 | MCP write | Too little: the write succeeds and the auto-commit fails | `commit_error`; the write stays uncommitted. | Nuisance |
| E74 | MCP | Too much: one long call (export up to about 600 s) | Every other tool call waits. | Degraded |
| E75 | Read-only (GUI) | Lost: an edit path that doesn't check the window's flag; the service under it is writable | Edits in a window the user believes can't change anything. | Data loss |
| E76 | Read-only server | Lost: a write tool without the guard | Caught by the read-only service, except for writes outside it (export). | Data loss |
| E77 | Changed on disk | Lost: checked only on window activation | E4 and E46 follow. | Data loss |
| E78 | Changed on disk | Too much: a banner after the window's own actions | Handled for own edits and draw.io saves. | Nuisance |
| E79 | Error reporting | Lost: expected failures show in the status bar for a few seconds and aren't logged; the log has no version | A missed message (E55); a field failure can't be tied to a version. | Degraded |

### Failures that take out several functions at once

| ID | Shared cause | Functions hit together |
|---|---|---|
| C1 | `DocumentService`, used by the GUI, MCP and `mellon-check` | Every answer. The gate can't catch a wrong answer the service gives everywhere (E41). |
| C2 | The loader | A misparse reaches validation, trace, coverage, export and the working-copy Compare at once; a parse error removes the document from all of them (E9, E10). |
| C3 | The suspect rule (`link_index`, `last_commit_for_paths`) | Scan, ack, review, export warning and `mellon-check`, which runs the same code without the cache. |
| C4 | Repository state (merge, `index.lock`, detached or unborn HEAD) | Commit, discard, tags, suspect states. |
| C5 | The repository root | Everything, on the wrong data (E14). |
| C6 | Clocks | Commit times decide merge renumbering (E59); file times drive the parse cache and the changed-on-disk check (E12, E77). |
| C7 | Programs holding files (antivirus, sync clients, viewers, draw.io) | Every write, through the 5 × 50 ms retry (E1). |
| C8 | The GUI's one cache for all windows | One stale entry is wrong in every window (E11). |
| C9 | A pygit2 or lxml upgrade | Every git-backed function; byte output of every write (E6). |

### Foreseeable misuse

| ID | Misuse | Leads to |
|---|---|---|
| M1 | Resolving document conflicts in a git client | E61 |
| M2 | Squash or rebase merges | E31 |
| M3 | Acknowledging without reviewing, or a whole document at once | E32, E33 |
| M4 | An agent and a person editing one document at the same time | E4, E5, E17, E46 |
| M5 | Editing files by hand or pulling while Mellon is open, without F5 | E4, E11 |
| M6 | Committing over MCP without `validate_repo` | E37 |
| M7 | Exporting with uncommitted edits and sending the PDF as an issue | E66 |
| M8 | `git push` without `--tags` | E57 |
| M9 | A clone on OneDrive or a network drive | E1, E15 |
| M10 | Starting Claude Code in the wrong folder | E14 |
| M11 | Accepting merge defaults unread | E64 |

## Requirements needed (H3)

Every data loss and wrong answer above, with the area document whose requirements must prevent or handle it. None of these is covered by a requirement yet. When an area is specified, its author turns each row into requirements (marked `derived` where they come from here) or records why the behaviour stays, and the row then names the requirement IDs.

| Effects | Area | What the requirements must settle |
|---|---|---|
| E2, E3 | `MELLON-STORE-SRS` | What an operation across several files leaves after a kill or power loss, and how it is noticed at the next start. Whether files are flushed. |
| E5, E17 | `MELLON-STORE-SRS` | What happens when two processes write one document at once: refuse, detect or merge, not lose a change silently. |
| E12 | `MELLON-STORE-SRS` | That a changed file is always read again. |
| E13 | `MELLON-STORE-SRS` | That a document folder Mellon won't use is reported, not ignored. |
| E14 | `MELLON-MCP-SRS` | How the server shows which repository it serves. |
| E16, E51, E52 | `MELLON-EDIT-SRS` | That an object ID is never given to a second object, as `definitions.md` already says, including after the highest-numbered object is deleted. |
| E18, E21 | `MELLON-EDIT-SRS` | Confirmation and undo for destructive edits, on each surface. |
| E19 | `MELLON-EDIT-SRS` | What a schema change does to existing values, and what it tells the user first. |
| E10 | `MELLON-TRACE-SRS` | That trace, incoming links and coverage say when a document couldn't be read (search: `MELLON-EDIT-SRS`). |
| E27, E30 | `MELLON-TRACE-SRS` | What a new link's ack is, and what uncommitted target edits mean for link states. |
| E28 | `MELLON-TRACE-SRS` | That a link whose state can't be worked out is never shown as OK. |
| E29 | `MELLON-TRACE-SRS` | Which changes to a target make a link suspect; that the stored index can't make a changed target read OK; how long a hash is compared. |
| E32, E33, E35 | `MELLON-TRACE-SRS` | That an ack records what was reviewed and accepts nothing newer; the default scope of an ack. |
| E43, E44 | `MELLON-TRACE-SRS` | Whether suspect and broken links count as coverage; how a cut trace is marked. |
| E36, E46 | `MELLON-VER-SRS` | That a commit holds what was validated and shown to its author, and nothing else. |
| E37 | `MELLON-VER-SRS` | Whether every commit path validates, MCP included. |
| E38, E40, E41 | `MELLON-CHECK-SRS` | Which link problems are validation errors; what `mellon-check` does with a root that has no documents; how the gate is kept independent enough to catch a wrong answer. |
| E47 | `MELLON-VER-SRS` | Commits on a detached HEAD. |
| E49 | `MELLON-VER-SRS` | What Discard removes and what it warns about first. |
| E53 | `MELLON-VER-SRS` | Which document changes Compare lists. |
| E55, E56, E57 | `MELLON-VER-SRS` | That an issue tag lands on the commit it was asked for, and that a failed tag is reported so it can't be missed. |
| E59, E60, E61 | `MELLON-VER-SRS` | Which object keeps its ID in a merge, and what the user sees about renumbered and retargeted links. |
| E62, E63, E64 | `MELLON-VER-SRS` | What editing is allowed during a merge; that resolving never overwrites edits made since; how a field-merged object is flagged. |
| E66, E67 | `MELLON-PDF-SRS` | What an exported PDF says about uncommitted edits and unreviewed links. |
| E71, E72 | `MELLON-MCP-SRS` | That `affected_files` lists every file a call changed; what an agent can undo. |
| E76 | `MELLON-MCP-SRS` | That the read-only server changes no file. |
| E4, E11, E77 | `MELLON-GUI-SRS` | When the Document Editor checks for changes on disk, so an edit or a commit never acts on an old copy. |
| E75 | `MELLON-GUI-SRS` | That read-only mode makes no change, whichever action is used. |

## Findings from the architecture view (2026-11-05)

The first architecture view found these. Each is handled when its area is specified, as a `derived` requirement with a test. Only F4 was confirmed by running code. The rest are from reading it.

| ID | Area | Effect | Finding |
|---|---|---|---|
| F1 | VER | Data loss | Editing isn't refused while a merge is in progress, and resolving the merge rewrites every file both sides changed, so edits made since the merge stopped are lost without warning. |
| F2 | GUI | Data loss | A window checks for external changes only when it is activated. An edit can overwrite an agent's change to the same field, and a commit validates the cached copy but commits the files on disk. |
| F3 | STORE | Data loss | Two processes rewriting one manifest, or allocating an ID, at the same moment can lose an outline change or give two objects one ID. |
| F4 | EDIT | Wrong answer | Deleting the highest-numbered object and creating a new one reuses its ID, which `definitions.md` says never happens. Confirmed by a scratch run. Specified 2026-11-06 in MELLON-STORE-SRS-1, -2, MELLON-EDIT-SRS-1, -2 and MELLON-VER-SRS-64, -65; a merge git finishes on its own is open question Q8. |
| F5 | TRACE | Wrong answer | When the suspect scan fails, the GUI shows links as OK and exports without the suspect warning. |
| F6 | TRACE | Wrong answer | Trace, incoming links, coverage and search silently leave out a document that fails to load. |
| F7 | TRACE | Wrong answer | An acknowledgement records the target's latest commit, not the one the reviewer read, so a change made in between is accepted unseen. |
| F8 | PDF | Wrong answer | A PDF of uncommitted edits carries the current issue with no warning. |
| F9 | VER | Wrong answer | An issue tag goes on whatever HEAD is when the tag is made, not the commit just made, and a failed tag is shown only briefly. |
| F10 | TRACE | Wrong answer | A trace cut at its depth limit looks the same as one that ended. |
| F11 | TRACE | Wrong answer | Coverage counts suspect and broken links as covering. |
| F12 | CHECK | Wrong answer | `mellon-check` on a repository with no documents exits 0, so CI passes having checked nothing. |
| F13 | VER | Wrong answer | Compare covers objects only. Already specified: MELLON-VER-SRS-25, Proposed. |
| F14 | STORE | Data loss | The rollback journal is in memory and nothing is flushed, so a kill or power loss can leave an operation half done, and stray temporary files are committed next time. |
| F15 | GUI | Wrong answer | Read-only mode is enforced widget by widget over a writable service, so a new edit path that forgets the check edits a read-only window. No such path exists today. |

## Per-area analysis

Each section is added when its area is specified: every component and interface of [architecture.md](architecture.md) the area uses, how each can fail, what detects it and what the response is. Results that need handling become `derived` requirements in the area's SRS.

### STORE

Not done yet. Handed over from VER's Compare analysis: D9, D10, D14, D16, D17, D18 and D29, and widening "can't be read" (see [VER](#ver)).

### EDIT

Not done yet.

### CHECK

Not done yet.

### TRACE

Not done yet.

### VER

#### Compare

Per-component failure analysis of Compare (MELLON-VER-SRS-0 to -61) against [architecture.md](architecture.md), using checklist items F1-F6, F8, F9, F12 and X1-X3. It builds on E52-E54 and F13 and doesn't repeat them.

- **Components and interfaces covered:** the comparison engine (`diff.py`: version resolution, the version list and defaults, reading a version at a commit, reading the working copy, parsing, classification), the Compare dialog (thread-pool job, list, detail panes, copy), the MCP compare tool, the Document Editor and History tab entry points, and what they use: git refs, trees and blobs through pygit2, issue tags, the working copy files, the shared parser and model, and the global thread pool.
- **Evidence:** `proof: analysis` throughout. Only the documents in this folder were read: no code, no tests. Each row says what it assumes about the code; [Not checked](#not-checked) lists the assumptions.
- **Effects** as in [the effect table](#failure-analysis): data loss, wrong answer, degraded, nuisance. Three rows are **Security**: they disclose files outside the repository, or let a commit from another clone stop Mellon. That fits none of the four effects, so these rows are rated Security and need a requirement under X3 whatever their effect.
- Row IDs are `CF<n>`; derived requirements proposed here are `D<n>`, listed under [Derived requirements](#derived-requirements-f12).

##### Failures per component and interface (F1-F4)

| ID | Component / interface | Failure | What the user gets | Detected by | Response | Effect | Requirement or proposal |
|---|---|---|---|---|---|---|---|
| CF1 | Git refs: version resolution | Wrong value: a branch, `HEAD` or a re-created tag moves (pull, commit from another process, `git tag -f`) between resolving a version and reading its files, or between two reads of the same version (manifest, then objects) | One result built from two commits: half a change shown as the whole change | Nothing | None stated | Wrong answer | D1 |
| CF2 | Git refs: what a result names | Wrong value: a result names a version only by a name that moves later (latest commit, branch, remote-tracking branch, issue tag) | A review record "Issue 2 → main" that can't be reproduced, or that now means other commits | SRS-39 (Proposed) only while the dialog is open | SRS-41 shows names | Wrong answer | D2 |
| CF3 | Typed version name (GUI, MCP) | Wrong source: a name that is both a branch and a tag, or a hex string that is both a branch name and a commit prefix; git picks one without a warning | A comparison of a version the user didn't mean, labelled with the name they typed | GUI: SRS-10, if "more than one" is taken to include ref clashes. MCP: SRS-60 covers only "no commit" | Error (GUI) | Wrong answer | GUI: SRS-10 (test a branch/tag clash and a branch/prefix clash). MCP: D3 |
| CF4 | Typed version name (GUI, MCP) | Unasked: a revision expression: `HEAD~2`, `main@{yesterday}` and `@{1}` (reflog, local to one clone), `:/text` (newest commit whose message matches, found by walking the whole history), `x^{tree}` (a tree, not a commit), `a..b`; or a string starting with `-` | Resolves differently later or on another machine; a slow history walk; a tree compared without a commit. Option injection is possible only if a git program is ever run; Git access is pygit2, so none is run today | Nothing | None stated | Wrong answer | D4 (person) |
| CF5 | Working-copy marker (MCP, typed) | Wrong source: the string that selects the working copy is also a valid branch or tag name | An agent asking for the branch gets the working copy, or the reverse | Nothing | None | Wrong answer | D5 |
| CF6 | Version list (history walk, issue ancestry) | Late: thousands of commits to search for "each commit that changed the document" (SRS-7); an ancestor check for each of hundreds of issue tags (SRS-12, -13) | The dialog is slow to open; if the list is built on the GUI thread, every window freezes | Nothing | SRS-58 covers only a running comparison | Degraded | D6 |
| CF7 | Version list | Too much: thousands of commits listed | The list is hard to use; the user types a hash | Visible | SRS-9 | Nuisance | None |
| CF8 | Version list: labels | Wrong value: a branch or remote branch named like an issue (`DOC-issue-3`), or a commit whose message reads "Issue 3" | The user picks a branch or a commit believing it is the issue | Nothing | None | Wrong answer | D7 |
| CF9 | Issue tags: defaults (SRS-12, -13) | Wrong value: two issue tags with equal numbers in issue order (`DOC-issue-2`, `DOC-issue-02`); an issue tag force-moved or re-created | From is set to either one, labelled "Issue 2"; the issue now means another commit | Nothing | None | Wrong answer | D8 (person); D2 for a moved tag |
| CF10 | Issue tags: defaults | Lost: working out the current issue fails (git error) | From left unset, as if the document had no issue (SRS-14) | The user sees From unset | SRS-16 asks for a choice | Nuisance | None |
| CF11 | Git objects; working-copy files | Lost: a blob missing (a partial clone, `--filter=blob:none`, isn't shallow, so the root check passes), a corrupt pack, a listed commit pruned after a force-push; a working-copy file that can't be opened (permission, locked, a cloud placeholder offline) | Objects unreadable, or the whole comparison fails | SRS-27, if "can't be read" covers it; the definition covers only a file that "isn't a well-formed file of its kind" | SRS-27, -28 | Degraded | SRS-27, -28, with the definition change in [Definitions and questions](#definitions-and-questions). Root check for partial clones: `MELLON-STORE-SRS` |
| CF12 | Git trees | Wrong value: where an object file is expected, the tree holds a submodule or a folder | The read raises, or the comparison fails | SRS-27, if read errors map to it | SRS-27, -28 | Degraded | SRS-27 (test each entry kind); symbolic links: D14 |
| CF13 | Git trees and working copy: which files are objects | Wrong value: an object file named in another letter case or with another extension (`D-5.XML`, `d-5.xml`), or two files differing only in case (one is lost in a Windows checkout). On Windows the working copy matches names without case, a git tree with it | Counted as an object in the working copy and not at a commit, or the reverse: Added or Deleted with nothing changed, or an object left out of the comparison | Nothing; the loader reports naming errors only for the working copy | None | Wrong answer | D9 |
| CF14 | Object files at a version | Wrong value: an object file parses but holds an object ID other than its file name; two files hold one ID (a merge resolved in a git client, M1, E61) | The object shown under the wrong ID; one of the two dropped silently, or shown twice | Load error in the working copy only; nothing at a commit | None | Wrong answer | D10 |
| CF15 | Manifest outline at a version | Wrong value: the outline names one object twice (`OUTLINE_DUP`, E17) | Moved worked out from either position: a false Moved, or a move missed | Nothing | SRS-29 covers only a manifest that can't be read | Wrong answer | D11 |
| CF16 | Manifest outline at a version | Too little: an object file with no place in the outline at To (Added, Modified), or at From (Deleted) | SRS-35 and -36 give the row no place. A list built by walking the outline drops it while the count (SRS-33) includes it | SRS-31 requires the row | None | Wrong answer | SRS-31 (test it); D12 for the order |
| CF17 | Working copy, read during a comparison | Wrong value: files change while they are read: a structural edit across the manifest and object files, a pull, a rebase, a checkout | A mixed state: half a move or delete, an Added object outside the outline | SRS-39 (Proposed) only once a change comes after the result is shown | None | Wrong answer | D13 |
| CF18 | Working copy, file written in place | Wrong value: another program writes a file in place (draw.io saves figures in place; editors; sync clients) | Truncated XML: unreadable (SRS-27). Truncated SVG: a figure change that isn't real | SRS-27 for XML; nothing for SVG | SRS-39 once built | Nuisance | SRS-27, SRS-39 |
| CF19 | Working copy: links on disk | Unasked: a file or folder in a document folder is a symbolic link or a junction (checked out from a clone made on Linux or macOS, or with `core.symlinks`) | The content of a file outside the repository shown in the dialog and returned to an agent. A link to a FIFO or a device blocks the read forever; the GUI can't quit because it waits for pool jobs | Nothing | None | Security | D14 |
| CF20 | Working copy versus the Document Editor | Too little: text typed in the Document Editor isn't saved to disk when the comparison reads the working copy (export saves it first; the Compare flow doesn't say it does) | The user's latest edit is missing from a comparison with the working copy | Nothing | SRS-56 forbids the comparison from saving it | Wrong answer | D15 (person) |
| CF21 | Working copy versus commits: git filters | Wrong value: git LFS or another filter: commits hold pointer files, the working copy the real files | Every figure Modified between a commit and the working copy | Visible: every row | None | Nuisance | None; say in `USAGE.md` that filters aren't supported |
| CF22 | Parser (XML from any commit) | Unasked: a document type declaration with external or parameter entities, nested entity expansion, XInclude, an external DTD reference (draw.io's SVG carries one) | The content of a local file in a description, shown and returned to an agent; a network fetch; memory exhausted | Only the parser's defaults, if kept (not checked): current lxml doesn't resolve external entities and limits expansion | None | Security | D16, D17 |
| CF23 | Parser and reader: resource exhaustion | Too much: a blob of hundreds of MB in a document folder; a text node near the parser's 10 MB limit; nesting deeper than the parser's limit | Memory exhausted: the GUI crashes, or the MCP server dies; or the file is unreadable | Parser limits, if kept (not checked) | SRS-27 for depth | Degraded | D18 (person: value) |
| CF24 | Engine: classification | Wrong value: content the model doesn't carry differs: an element or attribute added by hand, by another tool or by a newer Mellon | The same content dropped at both versions: "no object changed" | Nothing. Equal blob IDs prove equality, not difference | None | Wrong answer | D19 |
| CF25 | Engine: normalisation | Wrong value: XML differences that SRS-22 doesn't list count as changes (`<a/>` against `<a></a>`, quote style, `&#65;` against `A`, the encoding declaration), for example after an lxml upgrade (C9) | Every object Modified with no field changed | Visible | None | Nuisance | Question Q-C1 (person) |
| CF26 | Engine: blob-ID shortcut | Wrong value: the shortcut stops at an unchanged object file while the object's links file or figure changed | A link or figure change missed | Nothing | None | Wrong answer | SRS-21, SRS-46: test "only the links file changed" and "only the figure changed" |
| CF27 | Engine: scope | Too little: the rules, views and filters files and any other file in the folder aren't compared. SRS-25 (Proposed) adds only metadata, parents and attribute definitions (E53 lists rules, views and filters too) | "No object changed" between two issues whose rules changed | SRS-34 says "no object", not "no difference" | None | Wrong answer | SRS-25 and D20 (person) |
| CF28 | Engine: identity across a merge | Wrong value: a Mellon merge between From and To renumbered an object (E59): `D-5` at From is `D-9` at To, and `D-5` at To is the other branch's object. From is an ancestor of To, so SRS-42 doesn't warn | `D-5` Modified in every field, `D-9` Added: the reviewer reads a rewrite that didn't happen | Nothing | None | Wrong answer | D21 (person) |
| CF29 | Engine: identity on one line of history | Wrong value: an ID given again after a delete (E16, F4) | Modified instead of Deleted and Added (E52) | Nothing | None | Wrong answer | `MELLON-EDIT-SRS` (F4) |
| CF30 | Engine: Link reviewed | Wrong value: an ack cleared, or set to a commit that doesn't exist, counts as Link reviewed | It looks as if a review took place | SRS-53 shows the commit at both versions | None | Nuisance | SRS-53 |
| CF31 | Compare dialog: the comparison job | Stops: the job raises something other than a name or read error (git error, out of memory) or never finishes | The previous result stays (SRS-11) with no message. A job that hangs keeps the GUI from quitting; killing it loses the undo history (E25) | Nothing | SRS-11 keeps the old result | Degraded | D22, D23 (person: value) |
| CF32 | Compare dialog: the global thread pool | Late: export jobs (up to about 600 s each) fill the pool on a machine with few cores | The comparison waits; the dialog looks hung | Nothing | None | Degraded | None as a requirement; architecture: a pool of its own. SRS-57 times exclude the wait |
| CF33 | Compare dialog: detail pane | Late: word marking of a very long description (SRS-47), or drawing a large or pathological SVG figure (SRS-51), on the GUI thread | Every window freezes | Nothing | SRS-58 covers only a running comparison | Degraded | D24 |
| CF34 | Compare dialog: figures | Wrong value: a figure that can't be drawn (not SVG, a pointer file, beyond what the SVG renderer supports) is shown blank | It looks as if the figure was emptied | Nothing | None | Wrong answer | D25 |
| CF35 | Compare dialog: description text | Wrong value: a description from another clone holds bidirectional controls or zero-width characters (the text cleanup applies only to text entered through Mellon) | Marked words that look the same at both versions; the order shown differs from the order stored | Nothing | None | Wrong answer | D26 |
| CF36 | Compare dialog: copy (SRS-61) | Too little: the copied rows don't carry From and To, or say that rows were hidden | A review record that is incomplete without saying so | Nothing | None | Wrong answer | D27, D28 |
| CF37 | Compare dialog: result after a change | Wrong value: a pull or an edit after the result was shown | A result for versions that are gone | SRS-39 (Proposed) | Mark out of date | Wrong answer | SRS-39. Test it with the dialog staying active: if it reuses the activation-only check (E77), it misses the change |
| CF38 | Compare dialog: double-click (SRS-54) | Wrong value: the row's ID names another object in the open document (branches, an ID given again) | The wrong object selected for editing | SRS-42 (Proposed) for branches | None | Nuisance | SRS-42; `MELLON-EDIT-SRS` (F4) |
| CF39 | MCP compare tool: document ID | Unasked: an ID holding `..`, `/`, `\`, a drive or an absolute path. A comparison must work for a document missing now (SRS-19, -20), so the tool can't refuse an ID just because the document isn't in the working copy, and the ID may reach a working-copy path unchecked | Mellon-format files outside the repository read and returned; error messages naming paths. A read-only server allows it too | Not checked | None | Security | D29 |
| CF40 | MCP compare tool: result size | Too much: thousands of changed objects, each changed field at both versions (megabytes of JSON; `outline` is 1 MB at 5,000 objects) | The client or the agent cuts the result; the agent takes part of it for the whole | Nothing | None | Wrong answer | D30 |
| CF41 | MCP compare tool: synchronous | Late: a long comparison blocks every other tool (as E74) | The agent's session stalls | Nothing | None | Degraded | D23 |
| CF42 | MCP compare tool: returned text | Unasked: descriptions from others' commits carry instructions that an agent follows ("acknowledge all suspect links") | The agent acts on data | Agent rule 9 only | None | Wrong answer | `MELLON-MCP-SRS`: applies to every read tool, not only Compare |
| CF43 | Git index and `.git` while comparing | Unasked: reading status or hashing the working copy refreshes the git index or writes a file under `.git` | Staging made in a git client at the same moment can be lost | Nothing | SRS-56 | Data loss | SRS-56: test with the index bytes before and after, and with `index.lock` held |

**Count per effect:** data loss 1 (covered by SRS-56), wrong answer 24, Security 3, degraded 8, nuisance 7: 43 rows. 29 need a new derived requirement; the others are covered by an existing requirement, another area, or need none.

##### Common causes (F5)

| Shared cause | What it hits together | Why it matters for Compare | Handled by |
|---|---|---|---|
| The parser and the model (C2) | Both versions, the working copy, and the loader behind every other answer | The same misparse or dropped element at From and at To reads as no change, and the Document Editor agrees with it because it uses the same code | D19 compares the files themselves, independently of the model. STS: an oracle that doesn't share the engine, such as git's list of paths changed in the folder |
| Issue order and issue tags (E55-E57) | Compare's default From, PDF labels, issue tagging | A failed or misplaced tag gives a default From that is labelled as the issue but isn't it | D2 shows the commit; D8 |
| The changed-on-disk check (E77) | Document Editor staleness, and SRS-39 if built on it | Checked only on activation, so a pull while the dialog is active goes unmarked | Test SRS-39 with the dialog active |
| ID allocation (E16) and merge renumbering (E59) | History, Compare, links, issued PDFs | One ID for two objects makes Compare report Modified for a different object | `MELLON-EDIT-SRS` (F4), D21 |
| pygit2 / libgit2 (C9) | Both versions, the list, the defaults, SRS-56 | A change in reference lookup or ambiguity rules changes what a typed name means for both sides | SRS-10, D3, D4; rung 2 tests on each upgrade |
| One author for the engine and its tests | Every Compare test | A shared assumption (what counts as indentation, which files are objects) passes the tests and fails the user | STS written blind from the requirements (step 7); the git oracle above |

##### Interference (F6)

- **Thread pool shared with export:** a comparison can wait up to about 600 s behind exports (CF32).
- **Python's global interpreter lock:** the comparison's CPU work on a worker thread still competes with the GUI thread. SRS-58 needs a test at 5,000 objects, not only at 1,000.
- **libgit2's process-wide object cache (256 MB):** a comparison that reads many blobs shares it with the suspect scan, so it can push the scan over the cliff `SCALING.md` describes. Degraded, `TBD` (not measured).
- **MCP:** one synchronous comparison starves every other tool (CF41).
- **Memory:** both versions parsed plus the result, held in the GUI process. The budget is `TBD` in the architecture (open point 2).

##### Working back from the worst effect (F8)

The worst result is **a reviewer signs off a changed document as unchanged, or signs off the wrong change.** It follows from any one of these:

- Changes outside objects aren't compared: CF27 (SRS-25, D20).
- Content the model drops at both versions: CF24 (D19).
- An object counted at one version only: CF13, CF14 (D9, D10).
- Another commit than meant: CF1-CF5, CF9 (D1-D5, D8, SRS-10).
- Another object under the same ID: CF28, CF29 (D21, EDIT F4), and branches (SRS-42).
- A result that no longer holds: CF17, CF37 (D13, SRS-39).
- Rows that don't reach the reviewer: hidden rows (SRS-38), a copy without them (CF36, D27, D28), a row dropped from the order (CF16, D12), a cut MCP result (CF40, D30).
- A failed comparison that leaves an old "no object changed" on screen: CF31 (SRS-11, SRS-41, D22).
- Something shown that differs from what is stored: CF34, CF35 (D25, D26).
- The shortcut skipping a links-only or figure-only change: CF26 (SRS-21, -46).

Software doing exactly what its requirement says still gives this result in one case: SRS-34's "no object changed" is literally true while the manifest, rules or views changed. SRS-25 and D20 close it.

##### Protections that could fail silently (F9)

| Protection | How its failure would show | How long unnoticed | Shares a cause with what it protects | Test it needs |
|---|---|---|---|---|
| SRS-27, unreadable at a version | A parser that recovers from broken XML reads a truncated file as a partial object: a plausible Modified with fields "(not set)", or no change when both versions are broken alike | Until a reviewer notices missing text | Yes: the same parser | A truncated file, a file with conflict markers, and invalid UTF-8, at a commit and in the working copy |
| SRS-10, ambiguous names | A clash resolved without an error | Indefinitely | Yes: git's lookup | Prefix against prefix, branch against tag, branch against hex prefix |
| SRS-39, out of date (Proposed) | No mark after a pull | Until the window is activated | Possibly: the activation-only check (E77) | A change while the dialog stays active |
| SRS-40, latest request only | An old result for new choices | Until the user reads SRS-41's labels | No | Two requests, the first finishing last |
| SRS-34, "no object changed" | Shown for an empty result that is wrong | Indefinitely | Yes: the same engine result | D19 and D20 give it an independent check |
| The blob-ID shortcut | Fails safe (a false Modified), except CF26 | — | — | CF26 |

##### Ways in and what is worth protecting (X1-X3)

**Worth protecting:** files outside the repository (an agent's transcript goes to its model provider, so anything returned to it leaves the machine); the GUI and MCP sessions staying usable; the integrity of a review, so that a change can't be made to look absent or different; the repository state (SRS-56).

| Way in | Who | Path to what is protected | What stops it today | Worst result | Requirement |
|---|---|---|---|---|---|
| Commits pulled from another clone: file content | Anyone who can push, or whose repository is cloned | Entities, external references, XInclude, huge files, deep nesting, pathological SVG, read by the parser and the figure renderer | The parser's defaults, if kept (not checked) | A local file's content disclosed; the GUI or MCP server stopped | D16, D17, D18, D23 |
| Commits pulled: names and modes | Same | Symbolic links (on checkout), submodules, case-only clashes, unknown extensions, IDs that don't match file names | Nothing at a commit | Disclosure; objects miscounted (a change hidden on purpose) | D9, D10, D14 |
| Commits pulled: text | Same | Bidirectional and zero-width characters in descriptions | Nothing (the cleanup runs only on Mellon's own input) | A change shown as something else | D26 |
| Refs and tags pulled | Same | A branch named like an issue; a re-created issue tag; equal issue numbers | Nothing | The reviewer compares the wrong version | D2, D7, D8 |
| MCP arguments | The agent, or text that steers it | A document ID with a path in it; revision expressions; the working-copy marker | Not checked | Disclosure, even from a read-only server | D4, D5, D29 |
| Text returned to an agent | Anyone who can push | Instructions in a description | Agent rule 9 | The agent acts on them | `MELLON-MCP-SRS` |

**Secrets (X3):** Compare holds no secret or key, makes no network connection and uses no credentials. The only X3 result is disclosure of local files, handled by D14, D16, D17 and D29 with `req-type: Security`. Triggering a failure on purpose (a crafted commit that hangs or crashes the comparison) is handled by D18 and D23.

##### Derived requirements (F12)

Each needs a test case in `MELLON-VER-STS`. "Person" marks a proposal that needs a person for intent, a value or an accepted risk; the others an agent can decide. Status `Proposed` where a value is `TBD` or a person must decide; `Accepted` is recommended for the rest once committed.

| ID | Requirement | Type | Handles | Rationale | Person |
|---|---|---|---|---|---|
| D1 | When Mellon compares two versions of a document, Mellon shall read each version other than the working copy from the one commit that the version named when the comparison started. | Functional | CF1 | derived: a branch or tag that moves during a comparison would mix two commits into one result that looks real. | No |
| D2 | Mellon shall report with each comparison result the commit hash that From and To each named when the comparison ran, or that the version was the working copy. | Functional | CF2, CF9 | derived: a branch, the latest commit or a re-created tag later names other commits, so a review record that holds only names can't be checked. | No |
| D3 | If a version given to the MCP server's compare tool resolves to more than one commit, the MCP server shall return an error naming the version. | Interface | CF3 | derived: SRS-60 covers only a version that resolves to no commit; git picks one of two clashing names without a warning. | No. SRS-60 says should; align the keyword |
| D4 | Mellon shall accept a typed version only as a commit hash, a unique prefix of one, a tag, a local or remote-tracking branch, the latest commit or the working copy. | Functional | CF4 | derived: revision expressions resolve by reflog, date or message search, so the same text gives another commit later or in another clone; a range or an option isn't a version. | **Yes**: whether agents may use forms like `HEAD~2`. Recommend no: an agent can take a hash from the history |
| D5 | If a typed version names both the working copy and a branch or tag, Mellon shall report an error naming the version. | Functional | CF5 | derived: a branch named like the working-copy marker would silently compare the wrong version. | No. Recommend a marker that git refuses as a branch name, which meets this outright |
| D6 | While the Compare dialog lists the versions to choose from, the Document Editor windows and the Compare dialog should respond to input. | Quality | CF6 | derived: listing the commits that changed a document walks the whole history, which takes seconds in a repository of thousands of commits. | No. Optional time limit `TBD` |
| D7 | The Compare dialog shall show with each version it offers whether the version is the working copy, the latest commit, an issue, a local branch, a remote-tracking branch or a commit. | Functional | CF8 | derived: a branch can be named like an issue tag, and a commit message can read like an issue. | No |
| D8 | If two issue tags of one document have equal issue numbers, Mellon shall report both tags instead of choosing one as the current issue or issue before. | Functional | CF9 | derived: issue order compares whole numbers, so `2` and `02` are equal and either could become the default From. Belongs with the issue requirements. | **Yes**: whether leading zeros are allowed at all. Recommend refusing them when an issue is cut, and reporting old ones as here |
| D9 | If a file in a document's folder at From or at To has a name the on-disk format doesn't define, letter case included, Mellon shall report the file and the version. | Functional | CF13 | derived: on Windows the working copy matches names without regard to case and a commit's tree with it, so one object can exist at one version only. Assumes files git ignores aren't part of the working copy. | No. `MELLON-STORE-SRS` should own the naming rule |
| D10 | If an object's file at From or at To holds an object ID other than its file name, Mellon shall report the object as unreadable at that version. | Functional | CF14 | derived: a commit isn't loaded, so nothing else checks the naming rule there. Two files can hold one ID only if one of them breaks this rule, so this also catches duplicate IDs. | No |
| D11 | If the outline at From or at To names one object more than once, Mellon shall report the object and the version, and report no object as Moved. | Functional | CF15 | derived: with two positions for one object, Moved would be guessed; same reasoning as SRS-29. | No |
| D12 | The Compare dialog should put each row whose object has no place in the outline it is ordered by last, in object ID order. | Quality | CF16 | derived: SRS-35 and -36 order rows by an outline, and an object missing from it would otherwise be dropped from the list or placed arbitrarily. | No |
| D13 | If the working copy of a document changes while Mellon reads it for a comparison, Mellon shall report with the result that the working copy changed during the comparison. | Functional | CF17 | derived: a structural edit, a pull or a checkout during the read gives a mixed state; reported rather than read again, as SRS-39 decided. | No |
| D14 | If a file or folder in a document's folder at a version is a symbolic link or a junction, Mellon shall report it as unreadable at that version instead of reading its target. | Security | CF19, CF12 | derived: a link checked out from another clone would show a file outside the repository, or block on a device. `MELLON-STORE-SRS` should state it for loading too. | No |
| D15 | While the Document Editor holds text typed but not yet saved, the Compare dialog should say that the working copy doesn't include the text. | Functional | CF20 | derived: the working copy is the files on disk, and SRS-56 forbids saving them for a comparison. | **Yes**: warn, or save the text before comparing (SRS-56 would then need an exception). Recommend warn. First confirm in the code that the text can still be unsaved when the dialog opens |
| D16 | If a file of a document at From or at To declares an entity, Mellon shall report the file as unreadable at that version. | Security | CF22 | derived: entities in a commit from another clone can pull in a local file or expand without bound; Mellon's format uses none. | No |
| D17 | When Mellon reads a file of a document for a comparison, Mellon shall read no other file or network resource that the file refers to. | Security | CF22 | derived: external DTDs (draw.io SVGs carry one), XInclude and external images in a figure would read outside the version. | No |
| D18 | If a file of a document at From or at To is larger than TBD MB, Mellon shall report the file as unreadable at that version without reading its content. | Quality | CF23 | derived: a huge blob in a document folder exhausts memory and stops the GUI or the MCP server. Not built yet. | **Yes**: the value, perhaps one for figures and one for XML. Recommend measuring the largest real figure and setting the limit well above it |
| D19 | When an object's files at From and at To differ other than in line endings, indentation or order of XML attributes, Mellon shall report the object as Modified or Link reviewed. | Functional | CF24 | derived: content Mellon doesn't model would read as no change at both versions alike (common cause with the parser); the files are the independent check. | No. Shown as Modified, naming the file |
| D20 | When Mellon compares two versions of a document, Mellon shall report each file in the document's folder, other than the manifest and the objects' files, that differs between From and To. | Functional | CF27 | derived: rules, views and filters files change what is valid and shown, and SRS-25 doesn't list them, so a reviewer could sign off a changed document as unchanged (E53). | **Yes**: whether reviewers want these changes listed. Recommend yes, at least by file name. With SRS-25 it closes E53 and F13 |
| D21 | When a merge between From and To gave an object a new object ID, Mellon shall report the object's object ID at From and at To. | Functional | CF28 | derived: renumbering in a merge changes what an ID names along one line of history, so From's object reads as rewritten. | **Yes**: it needs the merge to record what it renumbered, for example in the merge commit. Alternative: warn when a merge Mellon resolved lies between From and To |
| D22 | If a comparison fails, the Compare dialog shall show an error message naming both versions and the reason. | Functional | CF31 | derived: SRS-11 keeps the old result, and SRS-10 and SRS-26 cover only a bad name and a missing document, so other failures pass unseen. | No |
| D23 | If a comparison hasn't finished within TBD s, Mellon shall stop it and report that the comparison didn't finish. | Quality | CF31, CF41 | derived: a hung comparison keeps the GUI from quitting and blocks every MCP tool. Not built yet. | **Yes**: the value. Recommend one well above SRS-57's 2 s at 5,000 objects |
| D24 | While the Compare dialog prepares an object's changes for display, the Document Editor windows and the Compare dialog should respond to input. | Quality | CF33 | derived: marking words in a very long description or drawing a large figure can freeze every window; SRS-58 covers only a running comparison. | No |
| D25 | If a figure at From or at To can't be drawn, the Compare dialog shall say so in place of that figure. | Functional | CF34 | derived: a blank pane looks like an emptied figure. | No |
| D26 | When a description at From or at To holds a character that changes text direction or has no width, the Compare dialog shall mark the character. | Security | CF35 | derived: text from another clone skips the text cleanup, and such characters make what is shown differ from what is stored. | No. Text attributes too, if wanted |
| D27 | When the Compare dialog copies rows to the clipboard, the Compare dialog should copy with them the From and To that the result was found for. | Quality | CF36 | derived: the copy goes into a review record (SRS-61), which must be readable on its own (SRS-41). | No |
| D28 | While rows are hidden, the Compare dialog should copy with the shown rows the number of rows hidden. | Quality | CF36 | derived: a copy without hidden rows reads as the whole review. | No |
| D29 | If a document ID given for a comparison holds a character other than a letter, digit, hyphen or underscore, Mellon shall refuse the comparison, naming the ID. | Security | CF39 | derived: a comparison must accept a document missing from the working copy, so the ID isn't checked against existing documents and could name a path outside the repository. | No. Windows device names (`CON`, `NUL`) remain for `MELLON-STORE-SRS` |
| D30 | When the MCP server's compare tool returns a result, the MCP server shall return with it the number of objects with each change. | Interface | CF40 | derived: a large result can be cut by the client, and the counts let an agent tell. | Optional: whether to page large results |

Each is under 35 words with one keyword. D3, D5, D9 and D29 could be merged into existing requirements instead (SRS-60, SRS-10), but the wording wasn't to be re-reviewed, so they are kept separate.

##### Definitions and questions

- **"Can't be read"** (definitions.md) covers only a file that is there but malformed. CF11 needs it to also cover a file that is there but can't be opened or read: a missing blob, permission denied, a locked file, a cloud placeholder offline. Otherwise SRS-27 doesn't apply and the whole comparison fails, against SRS-28's intent. Proposed: "…when it is there but can't be opened, or isn't a well-formed file of its kind." No person needed.
- **Q-C1 (person):** should SRS-22 count as no change every difference that canonical XML removes, not only line endings, indentation and attribute order (CF25)? Recommend yes: otherwise an lxml upgrade marks every object Modified. D19 would then use the same wording.
- **Leading zeros in issue numbers** (D8), **the value for D18 and D23**, **merge records** (D21), **non-object files** (D20), **unsaved text** (D15), **revision expressions** (D4): as in the table.

##### Not checked

No code or tests were read, so each of these is an assumption, and each row above that depends on one is `proof: analysis`:

- Whether Mellon's parser keeps lxml's safe defaults: no external entities, no network, no huge trees, no recovery from broken XML (CF22, CF23, F9).
- Whether each version is resolved once (CF1).
- Whether the version list, the defaults, word marking and figure drawing run on the GUI thread (CF6, CF33).
- How objects are listed at a commit and in the working copy: case, extension, symbolic links, non-file entries (CF12, CF13, CF19).
- Whether the engine keys objects by ID or by file name (CF14).
- Whether the blob-ID shortcut covers the links file and the figure (CF26).
- Whether the MCP tool checks the document ID, and what string selects the working copy (CF5, CF39).
- Whether text being typed is saved before a comparison (CF20).
- How a worker's exception reaches the dialog (CF31).
- Whether any path runs a git program (CF4).
- What pygit2 does with each revision expression (CF4).
- MCP result size and memory at 5,000 objects (CF40, F6).
- Whether reading status writes the index (CF43).

##### Outcome (2026-11-07)

The derived requirements went into `MELLON-VER-SRS` with the released build. Those a person had to decide were decided on 2026-11-07: D2 and D15 are Accepted, D20 and D23 (30 s) Proposed, D21 Deferred, and the rest Proposed until the person accepts them.

| Proposal | Requirement |
|---|---|
| D1 | MELLON-VER-SRS-66 |
| D2 | MELLON-VER-SRS-67 |
| D3 | MELLON-VER-SRS-68 |
| D5 | MELLON-VER-SRS-69 |
| D6 | MELLON-VER-SRS-83 |
| D7 | MELLON-VER-SRS-70 |
| D11 | MELLON-VER-SRS-71 |
| D12 | MELLON-VER-SRS-77 |
| D13 | MELLON-VER-SRS-72 |
| D15 | MELLON-VER-SRS-73 |
| D19 | MELLON-VER-SRS-74 |
| D20 | MELLON-VER-SRS-75 |
| D21 | MELLON-VER-SRS-76 |
| D22 | MELLON-VER-SRS-85 |
| D23 | MELLON-VER-SRS-86 |
| D24 | MELLON-VER-SRS-84 |
| D25 | MELLON-VER-SRS-81 |
| D26 | MELLON-VER-SRS-82 |
| D27 | MELLON-VER-SRS-78 |
| D28 | MELLON-VER-SRS-79 |
| D30 | MELLON-VER-SRS-80 |

- **D4:** git's revision expressions stay; MELLON-VER-SRS-9 was widened to them, and D2 shows the commit each resolved to.
- **Q-C1:** yes. MELLON-VER-SRS-22 now counts a difference that is gone in a file's canonical form as no change (definitions: Canonical form).
- **D15:** MELLON-VER-SRS-56 now allows saving pending edits.
- **Handed to STORE**, because they hold for reading any document at any version: D9 (file names, letter case included), D10 (an object ID that differs from its file name), D14 (symbolic links and junctions), D16 (entity declarations), D17 (no reads outside the file), D18 (a file size limit), D29 (characters in a document ID), and widening "can't be read" to a file that can't be opened.
- **Handed to the issue requirements in VER:** D8 (two issue tags with equal numbers, and leading zeros).

### PDF

Not done yet.

### MCP

Not done yet.

### GUI

Not done yet.

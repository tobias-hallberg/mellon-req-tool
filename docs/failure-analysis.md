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

Not done yet.

### EDIT

Not done yet.

### CHECK

Not done yet.

### TRACE

Not done yet.

### VER

Not done yet.

### PDF

Not done yet.

### MCP

Not done yet.

### GUI

Not done yet.

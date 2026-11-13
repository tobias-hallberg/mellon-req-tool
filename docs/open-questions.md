# Open questions

Questions raised while specifying Mellon that only a person can answer: intent, values and accepted risks. Each has a recommendation. When a question is answered, the answer and the date go under it, and the requirement it affects records the reason in its rationale.

Format:

```
## Q<n>: <question>

- **Raised by:** <area or review>, <date>
- **Affects:** <object IDs>
- **Recommendation:** <what to do, and why>
- **Answer:** <the decision, date>, or open
```

## Q1: When does the Compare dialog open with From set to the issue before the current one?

- **Raised by:** VER pilot (moving Compare), 2026-11-05
- **Affects:** the Compare default-version requirements in `MELLON-VER-SRS` (formerly MELLON-SRS-13 and -14)
- **What's there:** the requirement says "when the latest commit is the document's current issue". The built behaviour is wider: when the document's own files are the same at the latest commit as at its current issue, even if commits to other documents came after. When the document has only one issue and hasn't changed since, From is left unset and the dialog asks for a version. No requirement says that, but a test checks it.
- **Recommendation:** take the built behaviour into the requirements: "When the document hasn't changed since its current issue, From is the issue before it", plus "if there is no issue before it, From is unset and the dialog asks for a version". A commit to another document doesn't change this one, so comparing the issue with the latest commit would show nothing.
- **Answer:** as recommended, 2026-11-06. The requirements say "changed since its current issue", which `definitions.md` defines.

## Q2: Which machine are Mellon's performance requirements measured on?

- **Raised by:** VER pilot (moving Compare), 2026-11-05
- **Affects:** the Compare timing requirement in `MELLON-VER-SRS` (formerly MELLON-SRS-36), and every later performance requirement
- **What's there:** `mellon/SCALING.md` says only "one Windows machine with antivirus scanning on", so a limit like 2 s can't be checked on another machine.
- **Recommendation:** record the reference machine in `SCALING.md` (CPU, memory, disk, Windows version, antivirus on), define it in `definitions.md` as that machine, and re-measure there when it changes. Shared CI runners are too noisy to gate on, so the timing stays a manual test case on that machine.
- **Answer:** as recommended, 2026-11-06. The requirement times the comparison itself, from start to result, between two commits of a 1,000-object document with each object's description and one attribute changed. The machine was recorded in `SCALING.md` on 2026-11-07, measuring 1.0 s for that comparison.

## Q3: The Compare review's other decisions (2026-11-06)

Raised by the requirements view of the Compare section and answered the same day, as recommended. Each affected requirement records it in its rationale.

- The working copy can be chosen as From as well as To, as built.
- Moved objects, table and figure changes, and unreadable objects are "shall", not "should".
- Comparing reports changes to the document's metadata, parent documents and attribute definitions. That is Proposed: not built yet. Until then the dialog says "no object changed", not "no differences".
- A shown result is marked out of date when a version it was found for changes. It isn't recomputed under the reader. Proposed.
- The version list stays as built. Tags are typed, not listed.
- When From and To are on divergent branches, the dialog warns that an object ID can name different objects. Proposed.

## Q4: Should editing be refused while a merge is in progress?

- **Raised by:** architecture view, 2026-11-05 (failure analysis F1)
- **Affects:** `MELLON-VER-SRS` (merging), `MELLON-GUI-SRS`, `MELLON-MCP-SRS`
- **What's there:** editing goes on during a merge. Resolving it then rewrites every file both sides changed, so edits made after the merge stopped are lost without a warning.
- **Recommendation:** refuse edits in the GUI and over MCP while a merge is in progress, with a message that says to resolve or abort the merge first. That is simpler, and safer, than detecting which files were edited since.
- **Answer:** open, for the VER area

## Q5: When should the GUI check for changes on disk?

- **Raised by:** architecture view, 2026-11-05 (F2)
- **Affects:** `MELLON-GUI-SRS`, `MELLON-VER-SRS` (commit)
- **What's there:** only when a window is activated. An agent can change a file while the window stays active.
- **Recommendation:** check before each write and before each commit, and refuse with the "Changed on disk" banner as now. A commit should also validate what it commits, not the cached copy.
- **Answer:** open, for the GUI area

## Q6: Should an acknowledgement record the commit the reviewer looked at?

- **Raised by:** architecture view, 2026-11-05 (F7)
- **Affects:** `MELLON-TRACE-SRS`
- **What's there:** acknowledging records the target's latest commit at that moment, so a change made between reading and acknowledging is accepted unseen.
- **Recommendation:** yes. The review returns the commit it showed, and acknowledging takes that commit and refuses if the target has changed since.
- **Answer:** open, for the TRACE area

## Q7: How are object IDs kept from being reused?

- **Raised by:** architecture view, 2026-11-05 (F4, confirmed)
- **Affects:** `MELLON-EDIT-SRS`, `MELLON-STORE-SRS` (format)
- **What's there:** a new object gets one more than the highest number in the document now. After the highest-numbered object is deleted, its ID goes to the next new object, and links on other branches, history, Compare and issued PDFs then tie the old ID to a different requirement.
- **Recommendation:** keep the next free number in the manifest (a format change, so it needs the format version too). A new object takes the larger of that and one more than the highest number present, which also covers a manifest from before the change.
- **Answer:** fix it, 2026-11-06. Specified in MELLON-STORE-SRS-1 and -2, MELLON-EDIT-SRS-1 and -2, and MELLON-VER-SRS-64 and -65: the manifest's `next-object-number` attribute, raised on each create and kept by a merge. No format version was needed: a manifest without the number counts as 0, and Mellon 0.3.0 keeps the attribute when it rewrites a manifest (checked with the released build). 0.3.0 doesn't raise the number, so while a team mixes versions, a 0.3.0 user can still reuse a deleted ID.

## Q8: What about a merge that git finishes on its own?

- **Raised by:** blind test author for the ID-reuse fix, 2026-11-06 (analysis, not run)
- **Affects:** `MELLON-VER-SRS` (merging), `MELLON-CHECK-SRS`
- **What's there:** one branch creates DOC-3 and deletes it; another creates DOC-3. Both raise the next object number from 3 to 4, so git merges the manifests cleanly, adds the other branch's DOC-3, and commits without stopping. Mellon's merge resolution never runs, and DOC-3 now names an object other than the one the first branch gave it to. The same holds for any two branches: each gives out numbers the other can't see until they meet, and only a conflict (both create the same ID) lets Mellon renumber.
- **Recommendation:** accept it as a limit and say so in the definitions: an object ID is unique along one line of history, and a merge keeps it unique where git stops for Mellon. Catching the clean case needs a walk through both branches' history for IDs that were deleted on one side and created on the other, at every merge or in `mellon-check`. That costs more than the case is worth while branches live for days, not months.
- **Answer:** open

## Q9: The Compare blind tests' questions (2026-11-07)

- **Raised by:** the blind test authors for Compare, 2026-11-06
- **Affects:** `MELLON-VER-SRS-27`, `-40`, `-45`, `-47`, and `definitions.md`
- **Answers, with the person on 2026-11-07:**
  - **Words:** each punctuation mark is a word of its own, as built, so `stop.` → `stop!` marks only `.` and `!`. The definition of Word changed.
  - **Unreadable files:** an object whose unreadable file has the same content at both versions isn't listed. MELLON-VER-SRS-27 was narrowed.
  - **Labels:** `type` is shown as `Type` when the manifest gives it no label, as everywhere else in Mellon. The definition of Label changed.
  - **Typed versions:** typing into From or To changes it, before Enter or Compare. So a comparison running when the user types is dropped when it finishes, and Compare is enabled again. Added as the definition "From or To changes"; the dialog was changed to match.
- **Defects found and fixed:** Moved didn't take the largest same-order set of siblings (MELLON-VER-SRS-23), and re-comparing with a later row selected raised an error (MELLON-VER-SRS-31).

## Q10: The CHECK specification's questions (2026-11-10)

- **Raised by:** the CHECK author, its requirements view and its failure analysis, 2026-11-10
- **Affects:** `MELLON-CHECK-SRS`, by the IDs below
- **How they were handled:** the person wasn't available that night. Each question was answered with its recommendation, but a requirement that would change built behaviour was created as `Proposed`, so nothing is built until the person accepts it. Built behaviour stays `Implemented` or `Accepted` as written. These answers are for the person to confirm.
- **Answered with the recommendation (2026-11-10, to confirm):**
  - A required value that is blank (whitespace or characters with no width) counts as missing.
  - Integer values: ASCII digits with an optional minus sign. Real values add an optional point and ASCII digits. No plus sign, spaces, underscores, exponent, infinity or NaN.
  - A link to a document it may not link to, and a broken link, are validation errors; a suspect link isn't. `mellon-check` and `repo_health` report a broken link once.
  - A declared parent document that isn't in the repository, a cycle of parent documents, an enumeration with no option and an option on another kind are validation errors.
  - Table layout is checked when the document loads (STORE), so CHECK has no table codes.
  - The `requirement-type` alias in rule conditions stays as built.
  - An empty value counts as not set for a rule's required and forbidden directives.
  - A rules file that can't be read is a load error. A rule that names an undefined attribute or a value that doesn't fit, and an element the rules file format doesn't define, are validation errors.
  - `mellon-check` on a repository with no documents exits with status 2.
  - The gate may share the loader and validator with the GUI and MCP server. Link states get two checks by other means (`MELLON-CHECK-SRS` derived requirements) instead.
  - A pull request that deletes a whole document nothing links to passes the gate: accepted risk, since the deletion is the largest change in the diff and `mellon-check` lists the documents it checked.
  - Commit checks belong to VER.
- **Still open, each with a recommendation:**
  - -115: keep `CLAUDE_PROJECT_DIR` as `mellon-check`'s default root? Recommendation: no; check `--repo-root`, else the current folder.
  - -36: no `LINK_TARGET_MISSING` (and no link state) for links into a document with a load error? Recommendation: yes; the load error alone fails the gate.
  - -54: rule condition values compare as text (`1.0` isn't `1`)? Recommendation: yes.
  - -18: integers limited to -2^63 to 2^63 - 1, with leading zeros and `-0` accepted? Recommendation: yes.
  - -5: may a validation error code be retired? Recommendation: yes, in the changelog, never reused.
  - -40: a parent document declared twice is a validation error? Recommendation: yes.
  - -43: an empty or absent metadata date is valid? Recommendation: yes; the PDF prints no date.
  - -119: `mellon-check` exits 2 for a partial clone, and accepts a linked worktree's root? Recommendation: yes to both.
  - -83: the Validate action saves pending edits first? Recommendation: yes.
  - -66: a rule's link demand can't exempt headings. Recommendation: keep, and say so in USAGE.
  - -15: each check that tightens validation goes in the changelog's upgrade notes? Recommendation: yes.
  - -111: a time limit for `validate_repo`? Recommendation: yes, measured first.
  - -44: the codes drafted for new checks? Recommendation: yes.
  - -48: the next-object-number check skips a manifest without the attribute? Recommendation: yes.
  - -146: Mellon's own CI also runs the last released `mellon-check` over the specification? Recommendation: yes, and both must pass.
- **Answer:** open

## Q11: The TRACE specification's questions (2026-11-10)

- **Raised by:** the TRACE author, its requirements view and its failure analysis, 2026-11-10
- **Affects:** `MELLON-TRACE-SRS`, by the IDs below
- **How they were handled:** as for Q10: answered with the recommendation that night, with anything that would change built behaviour created as `Proposed`, for the person to confirm. Where a Proposed requirement would replace a built one, both are in the document, and each rationale says which replaces which.
- **Answered with the recommendation (2026-11-10, to confirm):**
  - Each link has exactly one state, the first that applies of unknown, broken, suspect and OK. A link whose state can't be worked out is never shown as OK.
  - A link to a target that exists but was never committed is suspect, and acknowledging it is refused until the target is committed.
  - Acknowledgements record the full commit hash, after one release that reads both forms. Until then each recorded digit is compared, and an ambiguous prefix makes the link suspect.
  - A change only to how a target's file is written (its canonical form is the same), moving the target in the outline, and changes to the target document's manifest or headings don't make links suspect.
  - Open question Q6 is adopted: an acknowledgement records the commit the reviewer saw, and is refused if the target changed since. The same goes for creating a link. A link key without a commit is accepted for one release, with a warning.
  - `ack_suspects` without `link_keys` stops acknowledging the whole document; in the GUI, Ack selected works on one object at a time, and a read-only window can't acknowledge.
  - An acknowledgement is a claim anyone with write access can make; git history is the record. Accepted risk.
  - Removing a link and adding it back keeps its review. A copied link keeps the acknowledged commit of the link it copies. Retyping a link keeps its review.
  - Coverage counts only OK links and reports separately the objects covered only through suspect or unknown links; it can be limited to some objects of the covering document; in transitive coverage with a link type, only the link into the covering document must have that type; 100 % only when every counted object is covered.
  - Trace depth is counted as the `trace` tool describes it. A trace follows an object's links once, on a shortest path, and marks the object on the other paths. The trace view has no depth limit.
  - A link from an object to itself, and two links from one object to one target, are validation errors (`MELLON-CHECK-SRS`). `mellon-check` fails on a link whose state is unknown.
  - Link states are worked out again when the latest commit moves while a window stays active (time limit TBD).
- **Still open, each with a recommendation:**
  - -10: a copied object's link that can't be kept: copy without it and report it.
  - -15: link types compared exactly as written: yes.
  - -58: attributes whose changes don't make links suspect, as DOORS allows: not now (a format change).
  - -184: a transitive coverage chain may pass through the reported document: yes.
  - -162: of several shortest paths, the trace follows the one whose objects come first in object ID order.
  - -117: with no acknowledged commit, `review_suspect_link` lists each commit that changed the target, capped.
  - -143: a read-only server accepts an `ack_suspects` dry run: yes.
  - -107: `suspects` results capped by a `max_results` argument, default 500.
  - -110, -111, -112, -98: the time limits, measured on the reference machine first.
  - -35, -36, -38, -85, -102, -165, -166, -180, -108: Proposed requirements that may already be built; the test mapping checks them. Does the state gutter mark incoming link states?
- **Answer:** open

## Q12: The STORE specification's questions (2026-11-11)

- **Raised by:** the STORE author, its requirements view and its failure analysis, 2026-11-10
- **Affects:** `MELLON-STORE-SRS`, by the IDs below
- **How they were handled:** as for Q10 and Q11: answered with the recommendation that night, with anything that would change built behaviour created as `Proposed`, for the person to confirm. Built behaviour stays `Implemented` as written, beside the Proposed requirement that would replace it.
- **Answered with the recommendation (2026-11-11, to confirm):**
  - Object IDs are limited to ASCII letters, digits, hyphen and underscore. No write can reach a path outside the document today, because every write needs an object that the document loaded from its file names; the rule holds by construction for created IDs.
  - A data file that breaks a format rule is a load error naming the file and the rule, except where `MELLON-CHECK-SRS` makes the break a validation error.
  - The written form is pinned: element order, XML attribute order, quotes, escaping (`&#10;` and `&#9;` in XML attributes) and empty elements. Where 0.3.0's choice isn't documented, the rationale says to confirm it, and the attr and link element orders are TBD.
  - An operation based on a copy that changed on disk since it was read, and creating a file over one that exists, are refused. A second Mellon process's operation waits up to a time limit (TBD), then is refused. Writers outside Mellon are an accepted risk.
  - Whether a file changed is decided by its content, not only by its modified time and size.
  - Each file is flushed to disk before it replaces the old one. An operation left half done is reported when the repository next opens, with an offer to put the files back.
  - A document folder inside another document's folder is a load error, counting only files git tracks. A document folder with object files and no manifest is a load error. Document IDs and object file names that differ only in letter case are reported.
  - Files the format doesn't define, and elements and XML attributes it doesn't define, are validation errors. Unknown elements are still kept when Mellon rewrites a file.
  - Data files and figure files have size limits (TBD), and data files a nesting limit (TBD).
  - Symbolic links, junctions, pipes and devices in a document folder are load errors, without opening them. Mellon refuses to write through a symbolic link.
  - Mellon reads and writes only inside the document folders, and checks the IDs that data files name before using them as paths.
  - Document IDs: ASCII letters, digits, hyphen and underscore, no Windows device name, greatest length TBD.
  - A DTD in a data file is a load error. An entity declared in a figure is a validation error, and is never expanded. Bidirectional control characters in stored text should be a validation error.
  - Comments count in a file's canonical form, and Mellon keeps them.
  - A folder holding a non-document `X/X.xml` stays a load error, as built.
  - A partial clone or sparse checkout that leaves out document files is refused as a repository root.
  - Whitespace around text is stripped when Mellon writes it, so that reading back gives exactly what was written. Stripping on read, as built, stays Implemented.
  - Integer and real values use the grammar `MELLON-CHECK-SRS` uses (Q10).
  - Acknowledged commits record the full hash after one release that reads both forms, as TRACE decided (Q11).
  - A figure that isn't well-formed SVG is a validation error (`MELLON-CHECK-SRS`). A view or filter name with no letter or digit gets the slug `view` or `filter`. Attribute names are limited so that a rules file can name them. The `requirement-type` alias stays as built.
  - An MCP tool call is all or nothing. Temporary files are never committed (`MELLON-VER-SRS`).
- **Still open, each with a recommendation:**
  - -96: ranges of integer values, real values and object numbers. Recommendation: integers and object numbers -2^63 to 2^63 - 1; reals the finite 64-bit IEEE 754 values. CHECK uses the same.
  - -209: a format version in the manifest? Recommendation: not yet. Add one with the first addition that can't safely be read as absent, and refuse to write a manifest newer than Mellon.
  - -69: must a manifest define the `type` attribute? Recommendation: no; without it a document has no headings.
  - -100: a table without rows, and a header row below a body row? Recommendation: no rows is a validation error, like no columns; a header row below a body row is valid.
  - -33: a view or filter file that can't be read is a validation error that leaves the view or filter out, not a load error for the document? Recommendation: yes.
  - -32: a load error for each data file that can't be read, not one per document? Recommendation: yes; the MCP server keeps `load_error` as the first and adds a `load_errors` list.
  - -67: remove a leftover `issue` element when Mellon rewrites a manifest? Recommendation: yes, as the one exception to keeping content Mellon doesn't model.
  - -195, -196: the order of an object file's attr elements and a links file's link elements. Recommendation: pin 0.3.0's order if it doesn't depend on edit history; otherwise attributes in the manifest's order and links by target document then target ID.
  - -184: reads also wait while another Mellon process writes the same document? Recommendation: yes.
  - -234, -235, -236: the size and nesting limits. Recommendation: well above the largest found in Mellon's own documents and the team repositories.
  - -225, -157: the greatest length of a document ID and a slug. Recommendation: short enough that the longest temporary file path stays under 260 characters for a root of up to 100 characters.
  - -169, -170, -185: how long to retry a file another program holds, and to wait for another Mellon process. Recommendation: retry at least 1 s and report within 5 s; wait for another process at least 10 s, or the longest bulk operation measured.
  - -239, -240: the time limits for opening and reloading a 1,000-object reference document. Recommendation: measure on the reference machine with the content-based change test in place, and set each at about twice the result.
- **Answer:** open

## Q13: The MCP specification's questions (2026-11-11)

- **Raised by:** the MCP author, its requirements view and its failure analysis, 2026-11-10
- **Affects:** `MELLON-MCP-SRS`, by the IDs below
- **How they were handled:** as for Q10 to Q12: answered with the recommendation that night, with anything that would change built behaviour created as `Proposed`, for the person to confirm. Built behaviour stays `Implemented` beside the Proposed requirement that would replace it.
- **Answered with the recommendation (2026-11-11, to confirm):**
  - The tool interface is in the specification: three tables (read tools; write tools; git, review and export tools) name each tool and parameter, with its type, whether it is required and its default. There is no separate interface file and no tool count.
  - A trailer goes on each commit the server makes, with Mellon's version in it (should).
  - Each write tool call is all or nothing. `partial` is returned only when restoring the files fails; when only the commit fails, the call returns `ok` true with `commit_error`.
  - The served root is named in the instructions and in each write result (should).
  - The server exits when standard input closes, after finishing the call in progress.
  - A relative `--repo-root` is read from the working directory, and an empty `CLAUDE_PROJECT_DIR` counts as unset.
  - The commit a write call asks for is refused when one of its files already had an uncommitted change. Commits return the paths committed, and a `--no-commit` start option (should) turns them off.
  - Unknown parameters and parameters of the wrong type are refused. Read tools fail with the same result shape as write tools. Paths in `affected_files` are relative, use `/`, and keep the file system's letter case.
  - The read-only server writes no file, `.git` included.
  - Writes are refused while a git operation is in progress (should; see `MELLON-VER-SRS`).
  - Invisible characters in stored text are flagged (should).
  - Size and time limits are TBD, to be measured first.
  - At log level `INFO`, logs hold only names, IDs and paths.
  - An empty or whitespace `commit_message` is refused; a missing one means no commit.
  - Tool names are kept while Mellon is 0.x (should).
- **Still open, each with a recommendation:**
  - -8: a linked worktree's root and a partial clone as the folder to serve? Recommendation: accept the first when its repository is a complete clone, refuse the second (as `MELLON-STORE-SRS` does).
  - -13: when the folder stops being a repository root, refuse later calls or exit? Recommendation: refuse each later call with the reason.
  - -24: which Model Context Protocol revision? Recommendation: the one the released build negotiates by default, recorded by the rung-3 test.
  - -29, -30, -65, -75, -77: the exit delay, start-up time, text size, result size and per-call time. Recommendation: measure the installed program on 10 documents of 1,000 objects first, then set each against the clients in use.
  - -31: the exit status for an unknown option. Recommendation: 2, as for an unusable root; standard error tells the two apart.
  - -55: `error_type` values for an unexpected failure, a missing item and a git operation in progress. Recommendation: `InternalError`, `NotFound`, `GitOperationInProgress`; an existing issue tag gives `AlreadyExists`.
  - -88: the full hash's field name, and whether the returned message holds the trailer. Recommendation: `hash`, and the message as committed.
  - -85: `include_trailer=false` once every commit has a trailer. Recommendation: refuse it from the next release, with the reason in the changelog.
  - -90: should `commit_changes` require `paths`? Recommendation: yes, from the next release (as `MELLON-VER-SRS` decided).
  - -105: does a cancelled or timed-out write finish or roll back? Recommendation: finish it and log it.
  - -99: an `ack_suspects` dry run on the read-only server. The MCP author recommends refusing it; `MELLON-TRACE-SRS-143` (Q11) recommends allowing it. Recommendation: allow it, since a dry run changes nothing and shows the reviewer what an ack would record.
- **Answer:** open

## Q14: The EDIT specification's questions (2026-11-11)

- **Raised by:** the EDIT author, its requirements view and its failure analysis, 2026-11-10
- **Affects:** `MELLON-EDIT-SRS`, by the IDs below
- **How they were handled:** as for Q10 to Q13. Where a Proposed requirement would replace a built one, both are in the document and each rationale says which replaces which.
- **Answered with the recommendation (2026-11-11, to confirm):**
  - Undo and redo never lower the next object number (-346). Undo puts the manifest back whole today, so undoing New object gives the undone object's ID again: a defect against -1 and -2, so -346 is Accepted and is being fixed.
  - A new object number is also above each number of the document that a link in the repository names, which covers Discard, a git reset and 0.3.0 clients. That an older client can still drop the number is accepted, with an upgrade note in the changelog.
  - A parent-document loop is refused. Move up, Move down, Indent and Outdent are disabled while rows are hidden. `reorder_outline` takes an optional outline it was based on and refuses a stale one.
  - `set_figure` refuses a file outside the repository; the GUI file chooser is unaffected. A file that isn't a regular file, and an SVG that declares an entity or loads another file or resource, are refused.
  - A filter condition on an undefined attribute, or an enumeration value that isn't an option, is reported instead of applied.
  - A default value goes only onto objects with no value for the attribute and whose rules allow it; the Properties dialog shows how many objects get it first (should).
  - A refused undo step names the file that changed and says git holds the committed copy. The undo history is cleared whenever the latest commit changes.
  - Values that `delete_attribute` and `remove_attr_option` remove are returned (shall). Showing, hiding and moving a column all save into the active view.
  - Pending edits land on the object, attribute or cell they were typed in, and a pending edit whose object is gone is kept and reported (EDIT, not GUI).
  - Deleting the `type` attribute or its Heading option is refused. When an edit hides the selected row, the next shown row is selected.
  - Tool annotations marking read-only and destructive tools are handed to `MELLON-MCP-SRS`.
- **Checked in the code that night:**
  - Every operation a Document Editor makes on its document's files is one undo step.
  - There is no drag-and-drop move or multi-select delete.
  - A new object under a parent becomes its last child.
  - `new_object` refuses bad links before writing any file.
- **Still open, each with a recommendation:**
  - -13, -14, -15: editing time limits at 1,000 objects. Recommendation: measure first, then about twice the result.
  - -144: the most copies one `copy_object` call may make. Recommendation: 100 unless a team needs more.
  - -119: should a default value also apply to objects created later, as in DOORS? Recommendation: no; the manifest keeps no default.
  - -25: should the default schema's `type` offer Requirement? Recommendation: yes.
  - -59, -97, -92: does the Properties dialog write each attribute and parent change at once, or on Apply? Recommendation: at once, each as its own operation; Apply covers metadata only, as built.
  - -226: may a header row follow a body row? Recommendation: yes (as Q12 recommends).
  - -249, -248: draw.io failing to start, or saving a file that isn't well-formed SVG. Recommendation: tell the user in both cases.
  - -271: does the filter box keep each found object's ancestor objects? Recommendation: yes, like a filter.
  - -305: do eq and ne on text ignore letter case? Recommendation: no.
  - -150: should `copy_object` copy links by default? Recommendation: keep it, as built.
  - -349: a size limit on the undo history? Recommendation: not yet; measure memory first.
  - Implemented requirements the documentation only implies, and Proposed ones that may already be built, are listed in the author's notes for the test mapping to check.
- **Answer:** open

## Q15: The VER specification's questions beyond Compare (2026-11-11)

- **Raised by:** the VER author, its requirements view and its failure analysis, 2026-11-10
- **Affects:** `MELLON-VER-SRS`: commits, uncommitted changes, history, issues and tags, merging, and working with git clients
- **How they were handled:** as for Q10 to Q14.
- **Answered with the recommendation (2026-11-11, to confirm):**
  - **Hooks and signing.** Mellon runs no git hook and no program that a repository's git settings name, as built. A commit that asks for signing is refused (Proposed). `mellon-check` in CI stays the gate.
  - **The `-merge` line.** `.gitattributes` marks document files `-merge`, so each file both sides changed goes through Mellon's resolver. A warning shows when the line is missing, and `mellon-check` fails without it (`MELLON-CHECK-SRS`).
  - **Commits over MCP.** `commit_changes` without paths commits only the server's own writes, and returns the paths it committed. An agent can't drop the trailer per call; a server start option that the person sets turns it off.
  - **Which object keeps its ID.** When both sides of a merge created an object under one ID, the object an issue tag reaches keeps the ID, then one that a remote-tracking branch reaches; otherwise the dialog asks. Commit dates don't decide. A renumbering is recorded in the renumbered object's own file, a format change for `MELLON-STORE-SRS`.
  - **Choices in the merge dialog.** A field both sides changed has no preset choice. Resolve stays disabled until each such field has one, with an explicit "Use ours for the rest". A link type both sides changed is asked about too.
  - **Links and issues.** Suspect or broken links don't block cutting an issue; the GUI warns, and `tag_issue` returns them.
  - **Tags across clones.** Two clones cutting the same issue, and fetches that prune unpushed tags, are an accepted risk. TEAM.md says to pull before cutting an issue and to push the tag at once.
  - **Commits during git operations.** Commits are refused during a merge, rebase, cherry-pick, revert or `git am`. Edits made after Resolve are staged, so the merge commit holds them. Filtered (LFS) files in document folders are refused for commit and discard.
  - **Duplicate IDs after a merge.** A merge that gave two objects one ID is detected in the Project Explorer, and `mellon-check` fails on it.
  - **Merge commits.** A merge commit changes an object when the object's files differ from each parent's, as the Last change definition of `MELLON-TRACE-SRS` says.
  - **Warnings and merge details.**
    - Warnings before a tag cover every document, with one requirement per kind of warning.
    - The merge banner shows in Document Editors too.
    - Objects whose creating commits carry the same change count as one object.
    - Parent documents merge like links: a one-sided removal wins.
  - **Worktrees.** Linked worktrees and submodule work trees stay accepted as roots, as `MELLON-STORE-SRS-16` decided.
- **Checked in the code that night:**
  - The resolver takes the one merge base that git's library picks.
  - The commit code can make a repository's first commit.
  - The MCP server has no discard tool.
  - The commit dialog's tag with nothing to commit is the free tag, not an issue.
- **Still open, each with a recommendation:**
  - -311: which merge base for a criss-cross history? Recommendation: the base content git recorded in the index for each conflicting file.
  - -175: the name of the start option that turns the trailer off. Recommendation: `--no-trailer`.
  - Compare requirements -60 and -68 to shall, and -59 to refer to the MCP compare tool (review Q59). Decide this with the person, since it changes committed Compare requirements and their test cases' links.
- **Answer:** open

## Q16: The PDF specification's questions (2026-11-11)

- **Raised by:** the PDF author, its requirements view and its failure analysis, 2026-11-10
- **Affects:** `MELLON-PDF-SRS`, by the IDs below
- **How they were handled:** as for Q10 to Q15.
- **Answered with the recommendation (2026-11-11, to confirm):**
  - **The page total.** It stays Implemented although both samples print it one short, and print "Page 1" on the cover and on the contents page. That is a defect to fix.
  - **Characters as typed.** The characters the samples prove print as typed are Implemented. A wider set of printable characters is Proposed, and characters outside it are reported. The samples show `'` printing as `’`.
  - **Exporting from the GUI.**
    - The GUI export reads the document, its figures and its parents as the working copy holds them at the export start.
    - The GUI warns before exporting uncommitted changes (should), and the export goes on.
    - Validation errors are counted on the cover and in the export warning; they don't refuse the export.
  - **The cover.**
    - It always shows the issue at the exported version, labelled Issue. The MCP tool's `rev` is a revision label shown beside it. A `rev` that resolves as a git version is refused.
    - The issue date is the issue commit's date; otherwise the manifest date is labelled Date.
  - **What counts as uncommitted.** Only files the on-disk format defines count as uncommitted for the cover. At an earlier version, the cover says link states weren't checked.
  - **Typesetting.** No hyphen is added at a line break, and text is ragged-right. A figure is drawn at its SVG size, 96 px to the inch. The page the SVG shows is the page drawn.
  - **Programs.** The minimum draw.io version is the oldest one a release was tested with. The source-checkout lookup of the PDF program in the project's `tools` folder is to be dropped, in favour of `MELLON_TECTONIC`.
  - **Refusals.** An export of a document with a load error, or during a merge in progress, is refused.
  - **The Traceability section.** It states how many outgoing links go to documents that aren't parents.
  - **One decision each:**
    - The MCP export, which asks no question, stays Implemented, while the cover counts and per-link marks that make such PDFs safe to read stay Proposed. These are review Q37, -50, -84 and -151.
    - The commit hash on the cover is accepted only together with the uncommitted-changes mark (review Q63, -80 and -83).
- **Still open, each with a recommendation:**
  - -71: should the 300 s limit grow with size? Recommendation: no; measure 1,000- and 2,000-object documents first.
  - -8: the responsiveness limit while an export runs. Recommendation: measure, and use the value `MELLON-TRACE-SRS-112` gets.
  - -67: drop the project `tools` lookup? Recommendation: yes, and say so in INSTALL.md's developer set-up.
  - -128: an empty attribute list given to the MCP export tool. Recommendation: no attribute.
  - -99: contents depth. Recommendation: three levels, once they are numbered.
  - -98: A4 only, or US Letter too? Recommendation: A4 only until a user asks.
  - -144: 0.3.0's table caption format. Recommendation: check the released build.
  - -88: committer or author date for the issue date. Recommendation: the committer date.
- **Answer:** open

## Q17: The GUI specification's questions (2026-11-11)

- **Raised by:** the GUI author, its requirements view and its failure analysis, 2026-11-10
- **Affects:** `MELLON-GUI-SRS`, by the IDs below
- **How they were handled:** as for Q10 to Q16. Undo and redo moved to `MELLON-EDIT-SRS`.
- **Answered with the recommendation (2026-11-11, to confirm):**
  - **Changed on disk.** "Changed on disk" is decided by content (`MELLON-STORE-SRS-181`). A Document Editor checks before each write, at the moment of commit, around each read, and every 5 s while shown (should, TBD once measured). Activation still raises the banner.
  - **Pending edits.** A pending edit that couldn't be saved is never dropped without asking.
  - **Read-only mode.** It ends only by the user's choice, and a reload keeps it. It forbids changing the git index and references, so a read-only window can't commit or tag, as built.
  - **Selection.** The moved, created or edited object stays selected, and the first visible row stays first.
  - **Errors.** If the error log can't be written, the error's text is shown in the dialog. The log is capped at 10 MB. An error dialog is shown at most once per 10 s.
  - **Themes.** Follow system follows the OS setting live (should), and is the first-launch default.
  - **The installer.** It doesn't downgrade, and refuses while a Mellon program runs; it never closes one. Nothing writes into the install folder at run time (should). Checking the installer by a build-provenance attestation is a could.
  - **Paths.** Paths with spaces and non-ASCII letters work. Paths past 259 characters are refused.
  - **Platforms.** Windows 11 64-bit (x64) is supported.
  - **Accessibility.** Suspect and broken links differ by shape, not only by colour (should).
  - **Accepted risks.** The gap between the check and the write, and a draw.io session left open when a window returns to read-only mode.
- **Checked in the code that night:**
  - Read-only mode disables Commit and the Properties dialog.
  - The outline pane shows each object.
  - Closing the Project Explorer closes each Document Editor and ends the GUI.
  - A commit returns only its own window to read-only mode.
  - The grid has single selection.
- **Still open, each with a recommendation:**
  - -8: does the installed, windowed program print its release number for `--version`? Recommendation: check; if not, withdraw -8 for the installed program.
  - -10: how many recent repositories to list. Recommendation: 10.
  - -23: where the error log is on Linux. Recommendation: read it from a source checkout on Ubuntu.
  - -61: what is selected after Delete object. Recommendation: the next object shown, or the one before.
  - -116: how many hash digits the status bar shows. Recommendation: at least 7.
  - -169 to -174: the performance and responsiveness limits. Recommendation: measure, then about twice the result.
  - -112: is 5 s right for noticing changes and refreshing the status bar? Recommendation: keep it unless the check takes over 0.5 s.
  - -213: drop Windows 10? Recommendation: yes; support each Windows 11 version still serviced at release.
  - -218: which Ubuntu release. Recommendation: the LTS that CI runs, pinned.
  - -198: what the installer does today while Mellon runs. Recommendation: check on rung 4.
- **Answer:** open

## Q18: Questions from testing the requirements (2026-11-12)

- **Raised by:** the test mappings and the blind tests of each area, 2026-11-11 and -12
- **Affects:** the requirement IDs below
- **How they were handled:** as for Q10 to Q17. A defect against an Implemented or Accepted requirement was fixed; where the product and a requirement disagreed and either could be right, the question is here.
- **Answered with the recommendation (2026-11-12, to confirm):**
  - `MELLON-EDIT-SRS-132` to `-134` now say both sides: an editor accepts an empty value and each fitting value, and no other value. `MELLON-EDIT-SRS-363` (Proposed) says an editor saves the typed value unchanged or refuses it.
  - `MELLON-EDIT-SRS-327` lists the type column with the state, ID and description columns, as built; `MELLON-EDIT-SRS-333` (a column-header control to show or hide a column) is Proposed, since 0.3.0 has none.
  - `MELLON-PDF-SRS-82`: an empty title is left out of the cover; the document ID doesn't stand in. `MELLON-PDF-SRS-147`: the leading header rows are those before the first body row, and they repeat even when a later header row follows a body row. `MELLON-PDF-SRS-54`: the export result's suspect links carry the same fields as the suspects tool's, `doc_id` included.
  - `MELLON-TRACE-SRS-100`: a jump selects the target in the target document's own Document Editor, since a Document Editor is the window for one document.
  - `MELLON-VER-SRS-91`: an editor side file is never committed, even when a path names it.
  - `MELLON-GUI-SRS-109`, `-111`: Changed on disk is decided by content, checked on activation, before each edit and before and after the Commit dialog, over the document's and its parents' folders. A file's digest is kept by its modified time and size and read again when either changes or within 2 s of the modified time, as git does; a change that keeps both outside that window isn't seen, the same gap the loader has. Undo and redo check their own step's files instead.
  - `MELLON-TRACE-SRS-6` against `MELLON-EDIT-SRS-150` (a copy keeps the links): copying an object one of whose links is broken is refused before anything is written, since the copy's link would be refused. `MELLON-EDIT-SRS-10` (Proposed: copy without the refused link and report it) is the planned answer.
  - `MELLON-STORE-SRS-182` (Proposed: refuse an operation whose files changed after Mellon read them) is built for the Document Editor, as part of `MELLON-GUI-SRS-109`; whether the MCP server and the Project Explorer meet it hasn't been checked. The code cites -109 only, so that a citation names a built requirement. Recommendation: build it for the other writers too, then mark it Accepted.
  - `MELLON-STORE-SRS-37`: since -109 refuses an edit or a commit after another program changed a file, the re-read that shows a load error then comes with F5, not with the edit or commit. The requirement holds as written; its rationale's "re-reads after an edit, a commit" no longer happen after an outside change.
  - Table editor: one Ctrl+Enter in a cell saves the cell's text and the table, as USAGE.md says and `MELLON-GUI-SRS-165` requires; the table editor stays open for more edits.
- **Still open, each with a recommendation:**
  - `MELLON-MCP-SRS-53`: for a missing parent declaration, link, figure or table, the server returns `error_type` `NotFound`, which no requirement lists, and existing tests expect it; the requirement says `EditorError`. Recommendation: keep `NotFound` and add it to `MELLON-MCP-SRS-55`'s table and to -53, since agents may already branch on it.
  - `MELLON-MCP-SRS-104`: a file rewritten with the same size and its modified time put back is answered from the old content, because parsed files are kept while their modified time and size stay the same. Recommendation: narrow -104 to changes that alter a file's size or modified time, and propose a content check separately if a case needs it.
  - `MELLON-GUI-SRS-74` (pending edits saved before any action) against Compare of two commits, which doesn't save (`MELLON-VER-SRS-56`, `-73`). Recommendation: keep -74 for every action that reads or writes the working copy, and name Compare of two commits as the exception. (The Read-only action now saves first, as recommended.)
  - `MELLON-TRACE-SRS-77`: a links file with an empty `target-id` is a load error for the whole file, not a broken link. Recommendation: keep the load error and say so in `MELLON-STORE-SRS`.
  - `MELLON-TRACE-SRS-17`: the link picker's link-type box is editable, so a user can type a type the document doesn't list. Recommendation: offer only the listed types.
  - `MELLON-EDIT-SRS-348`: with only a pending edit and no earlier step, Undo is disabled, so it can't be chosen. Recommendation: enable Undo while a pending edit exists.
  - The Views menu has no entry to go back to no view. Recommendation: add "(none)".
  - The Inspector shows no field for an attribute the object doesn't have set, so it can't set one (`MELLON-EDIT-SRS-125`). Recommendation: show every defined attribute.
  - Discard is disabled after F5 meets a load error, though the window is writable and the document has uncommitted changes. Recommendation: enable it; discarding is the way out of a broken file.
  - The Commit dialog committed a file holding conflict markers. Recommendation: refuse a commit while a document has a load error (`MELLON-VER-SRS`, Validation before a commit).
  - A document whose file gets conflict markers shows no Changed on disk banner on activation, though USAGE.md says the window keeps the last good copy behind it (`MELLON-GUI-SRS`, Changes on disk). Recommendation: show the banner.
  - `MELLON-STORE-SRS-30`: the merge-conflict note is added for a file holding only a `<<<<<<<` line. Recommendation: say "only when the file holds conflict markers".
  - `MELLON-STORE-SRS-132`: a draw.io figure whose `content` attribute is single-quoted counts as a draw.io figure, but draw.io reads no diagram from it. Recommendation: accept only what draw.io reads.
  - `MELLON-MCP-SRS-112`: whether storing a path-like `target_id` (add_link) or skipping one (ack_suspects) counts as accepting a path; `MELLON-MCP-SRS-62` and `-63` (Proposed) would settle it. Recommendation: accept -62 and -63.
  - `MELLON-TRACE-SRS-135`, `-152` and `-155` (the Inspector's Ack, Ack selected, `ack_suspects` with no keys acknowledge each suspect link) against `-130`: a suspect link whose target was never committed has no last change to record, so none of them acknowledges it (Q11). Today it gets no Ack button, Ack selected stays disabled, and `ack_suspects` with no keys leaves it out without a `skipped` entry. Recommendation: accept `-133`, or a narrower "If a link's target object has no last change, Mellon shall refuse to acknowledge the link", add "other than under -133" to -135, -152 and -155, and have `ack_suspects` with no keys list such links in `skipped`.
  - `MELLON-TRACE-SRS-48` (Proposed) is half built: its never-committed part is now covered by -45 and -47, but a target deleted at the latest commit and restored on disk, uncommitted (CF-T16), still gets that deleting commit as its acknowledged commit and reads OK, against the definition of last change, -4 and -43. Recommendation: build it (an object whose file isn't in the latest commit's tree has no last change, in the scan and when a link is created), and narrow -48's rationale to the deleted-and-restored case.
  - CI installs no PDF program, so every test that reads an exported PDF skips there. Recommendation: let CI download the same tectonic release the installer bundles and set `MELLON_TECTONIC`. Done as recommended on 2026-11-13, in the Linux job only (to confirm): the Windows job stays inside its time limit by skipping them.
- **Answer:** open

## Q20 (security): decisions from the code view's security findings (2026-11-13)

- **Raised by:** the code view of 2026-11-13. Its security findings (CV-GUI-04, CV-MCP-08, CV-PDF-06, CV-PDF-11, CV-TRACE-07, CV-TRACE-08, CV-VER-15, CV-VER-35) were kept out of `.review/code-view-findings.json` until fixed. Two audits that followed them (where repository text reaches the GUI as HTML, and where an ID becomes a path) found the rest of each pattern, and one more case.
- **Affects:** the requirement IDs below
- **How they were handled:** each was reproduced with a failing test, then fixed. Where the fix builds a Proposed requirement, the requirement was accepted on 2026-11-13 as a security fix, as Q11 did for `MELLON-TRACE-SRS-77` and `-79`.
- **Answered with the recommendation (2026-11-13, to confirm):**
  - `MELLON-PDF-SRS-67` and `MELLON-EDIT-SRS-247` are accepted (CV-PDF-11: an export ran a `tectonic.bat` committed to the repository it started in). The PDF program and draw.io are looked up in absolute PATH entries only, leaving out the start folder and anything inside the repository; a program named in `MELLON_TECTONIC` or `MELLON_DRAWIO` is used wherever it is. "The folder Mellon was started in" now says "directly in": the installed build starts in its own folder and runs the PDF program from the `tools` folder under it. The source checkout's `mellon/tools/` lookup is gone, as decided after the failure analysis; a source checkout sets `MELLON_TECTONIC` or puts tectonic on PATH.
  - `MELLON-PDF-SRS-27` (CV-PDF-06): fixed. A `\\?\` path into `.git` was written to, because the check compared it with the plain spelling; it now compares without the prefix and by each folder's file identity.
  - `MELLON-MCP-SRS-112` (CV-MCP-08): fixed. `git_history`, `remove_link` and `commit_changes` looked on disk at a `../` or share path before refusing it, which told the caller whether a file outside existed and made Windows contact a share's host. They now refuse it first.
  - `MELLON-STORE-SRS-229` is accepted (CV-TRACE-07: a document folder that was a link to another project made its objects show as incoming links). A document's folder or entry that is a link, junction, pipe or device is a load error, and the loader, link states, change detection and Compare don't follow it. A link directly under the repository root isn't followed either, so it isn't found to be a document and shows no load error; the alternative, a load error for each link at the root whose name is a document ID, would make `mellon-check` fail on a repository with an unrelated linked folder. Recommendation: keep.
  - `MELLON-STORE-SRS-230` is accepted (CV-TRACE-08: a parent ID naming a share made the link picker list it). The link picker picks among the documents that loaded, so a parent ID from a manifest never becomes a path.
  - `MELLON-VER-SRS-329` is accepted (CV-VER-35). Resolving a merge refuses, before writing anything, a path that isn't a document's manifest, rules file, object, links file, figure, view or filter, or that reaches one through a link. A view or filter file whose name isn't a slug counts as another path.
  - `MELLON-GUI-SRS-122` and `MELLON-VER-SRS-380` are accepted (CV-GUI-04: an image tag in an attribute value made the Inspector open the file it named; CV-VER-15 and CV-VER-35: View at this commit, the status bar and the Resolve merge dialog showed values, branch names and object IDs as formatted text). Each of the 83 places the audit found now shows repository and git text as plain text: labels the GUI builds are plain, message boxes, tooltips and prompts get text Qt can't take for HTML, and labels that are rich on purpose escape what they show. A test opens the main windows and dialogs over a repository full of markup and checks every label. The Compare dialog was already plain.
- **Still open, each with a recommendation:**
  - The merge-conflict note (`MELLON-STORE-SRS-30`) took the start of a load error as a file name, so an attribute name holding `..` could make every load name and read a file outside the repository (found by the path audit; fixed: a name with a `..` part, or a link, is never taken). No requirement says that loading reads nothing outside the document. Recommendation: add one to `MELLON-STORE-SRS`: when Mellon loads a document, Mellon shall open no file outside the document's folder.
  - `MELLON-STORE-SRS-228` (Proposed: refuse to write or delete through a link) is built for resolving a merge only. Recommendation: accept it and build it in the editor too.
  - `MELLON-PDF-SRS-31` (Proposed: refuse a device, pipe or colon path in `export_document`) is unchanged. Recommendation: accept it.

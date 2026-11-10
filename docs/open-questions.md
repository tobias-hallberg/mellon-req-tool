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

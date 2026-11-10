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

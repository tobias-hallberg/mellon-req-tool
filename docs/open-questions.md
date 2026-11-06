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
- **Answer:** as recommended, 2026-11-06. The requirement times the comparison itself, from start to result, between two commits of a 1,000-object document with each object's description and one attribute changed. Still to do: record the machine in `SCALING.md` at the next measurement there.

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
- **Answer:** open, for the STORE and EDIT areas

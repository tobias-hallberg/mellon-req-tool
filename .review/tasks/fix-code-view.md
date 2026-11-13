# Task: fix what the code view found

The code view compared every built requirement with the code, area by area, with the code checklist (C1 to C16 and C25). Its findings are in `.review/code-view-findings.json`, each with an `id` (`CV-<AREA>-<nn>`), `kind`, `item`, `where`, `path`, `impact`, `proof` and `fix`. The reviewers proved most of them with probe scripts that aren't in the repository; `path` says how to reproduce each one.

Your prompt names the areas you take. Work only on those areas' findings, on your own branch (`fix/cv-<areas>`, from `spec/ver-compare`), so that several sessions can run at once.

## For each finding

Read CLAUDE.md and `docs/specification.md` first.

1. **Reproduce it with a test** that fails because of the problem: in the test module of the source module at fault, named `test_<area>_srs_<n>_<what>` after the requirement it breaks. Run it and see it fail. If it passes, the finding is wrong or already fixed: say so in your report and move on.
2. **Then decide by kind:**
   - **`defect`** against an Implemented or Accepted requirement: fix the code until the test passes. Add an entry under `### Fixed` in `mellon/CHANGELOG.md`, naming the requirement.
   - **A defect whose fix changes what a requirement says**, or a `gap`, `ambiguity` or `question`: don't change the requirement, and don't change built behaviour to match a reading of it. Add the item to the open questions (below) with your recommendation. Mark its test `@pytest.mark.xfail(strict=True, reason="defect: <requirement>: <what>")` if it shows a real defect waiting on that decision.
   - **A requirement that is Proposed but already built** (the reviewers list some): add it to the open questions; don't change its status.
3. **List the new test** in the requirement's test case: add its node id to the test case's `automated-tests` through `tools/spec/released.py` (`set_attribute` on the `MELLON-<AREA>-STS` object), never by hand and never with the development build. When no test case verifies the requirement yet, create one (`new_object` with `type: Test Case`, then `add_link` with `link_type: verifies`).
4. **Keep the citations true.** If you move or rename code that carries a `# Requirements:` comment, move the comment with it.

## Open questions

Add one new question per session at the end of `docs/open-questions.md`, titled `Q19 (<areas>): decisions from the code view`, in the format of Q18: each item names the requirement and the finding id, says what the code does, and gives a recommendation. Its answer is `open`.

## Before each commit

From `mellon/`:
- `QT_QPA_PLATFORM=offscreen uv run pytest -n auto`, all passing (strict xfails count as passing).
- `uv run mellon-check --repo-root ..`, exit 0.
- `uv run pytest tests/test_mellon_spec.py`, passing.

Commit per finding or per small group, with the finding ids in the message. Push your branch and say in your report:
- what you fixed,
- what went to the open questions,
- which findings you found wrong, with the reason,
- what you didn't get to.

# Task: mutation testing

Measure whether the tests catch bugs in Mellon's core modules: change the code in small ways (mutants) and see whether a test fails. A mutant that no test notices is a missing test, an equivalent change or dead code.

Work on your own branch (`test/mutation`, from `spec/ver-compare`). Read CLAUDE.md first.

## Setup

mutmut runs on Linux (it forks), so this runs in a cloud session or WSL, not on Windows.

```
cd mellon
uv sync --all-groups
uv pip install mutmut
```

Don't add mutmut to `pyproject.toml`; it stays a tool you install for the run. Configure it on the command line or in a `setup.cfg` you don't commit. Limit each run to one module (`paths_to_mutate`) and its tests, with `QT_QPA_PLATFORM=offscreen`. Run the modules in this order: `suspects.py`, `link_index.py`, `validator.py`, `check.py`, `coverage.py`, `trace.py`, `diff.py`, `merge.py`, `editor.py`.

## For each surviving mutant

Sort it into one of three:
- **Missing test:** a change a user or agent could notice, which no test catches. Write the test in the module's test file, named after the requirement it protects (`test_<area>_srs_<n>_<what>`; find the requirement through the `# Requirements:` comment above the function). Check that it kills the mutant, then add its node id to the requirement's test case with `tools/spec/released.py` (`set_attribute` on `automated-tests`).
- **Equivalent:** the mutant behaves the same as the original for every input. Say why in one line.
- **Dead code:** no input reaches it. Don't delete it in this task; list it.

A survivor that shows the code breaking a requirement is a defect. Add a strict xfail test and an item in a new `Q19 (mutation)` question at the end of `docs/open-questions.md`, with a recommendation.

## Deliver

- **The tests**, committed per module. Before each commit run:
  - `QT_QPA_PLATFORM=offscreen uv run pytest -n auto`, passing,
  - `uv run pytest tests/test_mellon_spec.py`, passing.
- **`.review/mutation.md`**, with one section per module: mutants made, killed, survived, timed out. Each survivor goes in a table with its location, the change, its sort (missing test, equivalent, dead code) and the test that kills it.
- **Push the branch.** Your report says what was measured, the kill rate per module before and after, and what wasn't run.

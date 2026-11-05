# How Mellon specifies itself

Mellon's requirements and tests are kept in Mellon, in this repository, next to the code. New behaviour is specified before it is built, and existing behaviour is being specified area by area until the whole application is covered. This page records how that is done. The terms it uses are in [definitions.md](definitions.md), and open questions are in [open-questions.md](open-questions.md).

The approach follows a software engineering framework that is kept outside this repository. It draws on ISO/IEC/IEEE 29148, DO-178C and ISO 26262, but nothing here is a certification artefact or a claim of compliance.

## Documents

The specification is split by feature area, because a document is meant to stay under 1,000 objects. Each area has a requirements document and a test document, and the test document has the requirements document as its parent.

| Area | Requirements | Tests | Scope |
|---|---|---|---|
| STORE | `MELLON-STORE-SRS` | `MELLON-STORE-STS` | Repository rules, document discovery, load errors, the on-disk format, saving, text rules, path and XML safety |
| EDIT | `MELLON-EDIT-SRS` | `MELLON-EDIT-STS` | Documents, metadata, attributes, objects and the outline, tables, figures, search, saved filters and views |
| CHECK | `MELLON-CHECK-SRS` | `MELLON-CHECK-STS` | Validation, the rules engine, repository health, `mellon-check` |
| TRACE | `MELLON-TRACE-SRS` | `MELLON-TRACE-STS` | Links, incoming links, suspect and broken links, acknowledging, trace, coverage |
| VER | `MELLON-VER-SRS` | `MELLON-VER-STS` | Commits, discarding, history, issues, tags, Compare, merging |
| PDF | `MELLON-PDF-SRS` | `MELLON-PDF-STS` | PDF export |
| MCP | `MELLON-MCP-SRS` | `MELLON-MCP-STS` | The MCP server's general contract |
| GUI | `MELLON-GUI-SRS` | `MELLON-GUI-STS` | The GUI shell, installation and platforms |

- **A feature's requirements live in one area,** whichever way the user reaches it. Compare's dialog, its MCP tool and the comparison itself are all in VER. The MCP and GUI documents hold only what applies to the whole server or the whole GUI.
- **Each requirement is said once.** When two areas touch, it goes where a reader would look for it first, and the other area refers to it by ID if it needs to.
- **Split early.** When a document passes about 600 objects, split it at a heading into a new area, before it nears the limit. Moving an object to another document gives it a new ID.
- PDF export has a document of its own so that the exporter can be replaced without touching the rest.

## Requirements

### Form

`[Condition,] the <subject> shall|should|could <action> <object> [constraint].`

The subject is one named thing, such as "the Compare dialog", "the MCP server" or "Mellon". Start with one of these:

| Pattern | Start | Use for |
|---|---|---|
| Always | The <subject> shall … | Behaviour with no condition |
| Event | When <event>, the <subject> shall … | A response to something happening |
| State | While <state>, the <subject> shall … | Behaviour during a state or mode |
| Abnormal | If <fault or invalid input>, the <subject> shall … | A response to something going wrong |
| Option | Where <feature is present>, the <subject> shall … | Behaviour of a variant |

### How binding it is

Exactly one of these keywords appears in each requirement, and it's the only record of how binding the requirement is:

| Keyword | Meaning |
|---|---|
| shall | Mandatory. A release doesn't go out with it unmet. |
| should | Wanted. It can be left unmet only with a recorded reason that a person accepts. |
| could | Optional. It's built if it's worth it. |

Every `Accepted` or `Implemented` requirement is verified, whichever keyword it uses. Don't use "must", "may", "will", "might" or "would".

### Level

Mellon is one program: the GUI, the MCP server, `mellon-check` and the library run the same code. Nothing inside one program can be shown not to interfere with the rest, so the whole program has one level, the highest of anything it does. Mellon can lose, corrupt or silently change stored requirements, links and history, and reviews and release gates trust its answers (validation, suspect links, coverage, trace, Compare). A wrong answer that looks right is the worst failure. So Mellon is `High` throughout, and every requirement has `level: High`. Layout and shortcuts get the same checking as saving and merging.

### What, not how

Requirements have to survive a rewrite of Mellon in another language, or the replacement of one part, such as the PDF exporter. So a requirement states behaviour that can be observed at Mellon's boundaries, and never names a module, class, function, library or test framework.

- **These are requirements,** because they are interfaces or product decisions: the on-disk format, so that a rewrite reads the same repositories; git as the store, with its commits, tags and the suspect-link rule; the MCP tools; the command lines and exit codes; and what the user sees and can do in the GUI.
- **These are not:** how the behaviour is achieved, such as caches, threads or the order of internal steps. They belong in [architecture.md](architecture.md) or the code.

### Language

- Use positive statements in the active voice. "Shall not" is only for a real prohibition.
- Avoid vague pronouns (it, this, they), superlatives (best, most), subjective words (easy, intuitive), vague qualifiers (fast, appropriate), open-ended phrases (including, such as, etc.), loopholes (if possible, as needed) and totals (all, every, never). Name the exact set instead. Leave a pronoun where what it refers to is obvious.
- State quantities with units, a range and what is measured, and say whether the limit itself is in or out.
- Keep to 35 words. Longer usually means more than one requirement.
- Use the terms in [definitions.md](definitions.md), which is the only list of terms.
- Keep a requirement only if changing the behaviour would be a product decision: a user would notice, or stored data, a recorded value or an interface is at stake. Exact wording, layout and individual error branches don't belong.

### Each requirement is

Necessary, at the right level, unambiguous (two people would write the same test from it), complete on its own, singular (no part can pass while another fails), feasible, verifiable, correct, free of implementation, and written to this page.

## Attributes

### Requirements documents

Only `Heading` and `Requirement` objects go in a requirements document, because the coverage report counts any other object as needing a test.

| Attribute | Values |
|---|---|
| `type` | `Heading`, `Requirement` |
| `req-type` | `Functional`, `Performance`, `Interface`, `Security`, `Quality`, `Constraint` |
| `status` | `Proposed`: not decided<br>`Accepted`: we intend to build it, or it's built but not yet in a release<br>`Implemented`: built and in a release<br>`Deferred`: not planned for now<br>`Withdrawn`: dropped for good |
| `level` | `High` |
| `rationale` | Why it exists, any assumption, and where it came from, as the first word or words: `stakeholder:`, `analysis:`, `derived:`, `parent:` or `as implemented:` |

- **`as implemented` is a warning sign.** The requirement describes what the code does, so nothing has checked that the behaviour is right. Those are reviewed first.
- **`derived`** marks a requirement that comes from a design decision or the failure analysis, not from a user's need. Its rationale explains the decision.
- **Open values** are written `TBD`, only in a `Proposed` requirement, with a question in [open-questions.md](open-questions.md). A value is never invented.
- **IDs are never reused.** A committed requirement that is dropped becomes `Withdrawn` and stays.

The document's rules file makes `req-type`, `status`, `level` and `rationale` required on a requirement, and forbids them on a heading.

### Test documents

| Attribute | Values |
|---|---|
| `type` | `Heading`, `Test Case`, `Information`, `Note` |
| `status` | As for requirements |
| `verification-method` | `Test`, `Analysis`, `Inspection`, `Demonstration` |
| `execution` | `Automated`, `Manual` |
| `rung` | `1 Unit`, `2 Git`, `3 GUI and MCP`, `4 Installer`, `5 Person`. See [Test ladder](#test-ladder). |
| `automated-tests` | The automated tests that implement the test case, one per line, as pytest node ids relative to `mellon/` |
| `rationale` | Why the test case is shaped the way it is, where that isn't obvious |

- **A test case is written in product terms:** what to do and what to expect. Only `automated-tests` ties it to this implementation, so a rewrite keeps the test cases and replaces that attribute.
- Each test case links `verifies` to the requirements it checks.
- The rules file requires `verification-method`, `execution` and `rung` on a test case, a link to the area's requirements document, and `automated-tests` when `execution` is `Automated`.
- `mellon/tests/test_mellon_spec.py` fails when a listed node id names no test, and when an `Accepted` or `Implemented` requirement has no test case.

## Tracing

- A test case links `verifies` to each requirement it checks. Add the links only after the requirements are committed, or they show as suspect: a file can't record its own commit, so revising a requirement and acknowledging its test cases always takes two commits.
- Code that implements a requirement names it in a comment directly above, such as `# MELLON-VER-SRS-19: rows follow the outline at To.`
- Tests name the requirements they verify through the test case's `automated-tests`.

## Test ladder

| Rung | What runs | Run by |
|---|---|---|
| 1 Unit | Tests of the library | Agent |
| 2 Git | Integration with real git repositories, such as the `req-test` fixture, including merges, conflicts and an external git client changing files | Agent |
| 3 GUI and MCP | The GUI offscreen (`QT_QPA_PLATFORM=offscreen`), and the MCP server over stdio | Agent |
| 4 Installer | The frozen installer on a clean Windows machine, with an upgrade from the previous release | Agent, given a virtual machine |
| 5 Person | A person using a release candidate on a real project | Person |

Each feature is tested on rungs 1 to 4, wherever a rung can reach it. A finding on a higher rung goes back down as a test.

## Working on the specification

**Edit with a released build.** A bug in a development build could damage Mellon's own specification without anyone noticing. So the documents are edited through a released Mellon (its GUI or MCP server), and each change is also checked by scripts that read the XML directly, without Mellon's code. Rules files have no editing tool, so they are the one thing edited by hand.

**Each area is specified in this order,** one branch and pull request at a time. Each review is done by a fresh agent, which sees only what its view allows:

1. **Write the requirements** from what the product should do, using `USAGE.md`, `STATUS.md`, `TEAM.md`, `INSTALL.md`, the changelog and the tool descriptions. Behaviour in the last release is `Implemented`, behaviour built since is `Accepted`, and a gap is `Proposed`.
2. **Run the wording, level and validation checks.**
3. **Review the requirements.** The reviewer sees the requirements and the definitions, not the code or tests.
4. **Analyse failures** for the area against [architecture.md](architecture.md), in [failure-analysis.md](failure-analysis.md). Each result that needs handling becomes a `derived` requirement.
5. **Ask a person** about intent, values and accepted risks, each with a recommendation. Commit the requirements.
6. **Map the existing tests** to test cases. The mapper sees the requirements, the tests and their output, not the code. It lists requirements without a test, and tests that verify no requirement.
7. **Write the missing tests blind,** from the requirement and the public signatures alone. A test that fails is a defect, a requirement gap or a test error. A defect is fixed through the change routine, with its regression test first.
8. **Link and commit the test cases,** then check the tracing.
9. **Run mutation testing** on the code involved, and sort each surviving mutant as a missing test, an equivalent change or dead code.
10. **Review the code** against the requirements, without the tests, adding requirement comments to the code on the way.
11. **Record which reviews were done,** and update `STATUS.md` and the changelog.

**Changing a requirement later:**
1. Commit it, which turns the links to it suspect.
2. Read each suspect link with `review_suspect_link`, and update the test case, the test and the code if they no longer hold.
3. Acknowledge exactly the links you reviewed (`link_keys`), never a whole document at once, and commit again.

Validate the repository before every commit, because an MCP commit doesn't validate.

**Before a pull request,** these must be clean:
- the full test suite, including `tests/test_mellon_spec.py`
- `mellon-check --repo-root ..`
- the wording, level and tracing scripts, whose output goes in the pull request description because CI can't run them

## Deviations from the framework

- **One program across several requirements documents.** The framework gives each program one requirements document. Mellon's would pass 1,000 objects, so it's split by area. Every document still has the one level, `High`.
- **`Test Case`, not `Test`,** as the test object type. That's the IBM DOORS term, which Mellon's vocabulary follows.
- **`rung`** is an extra test case attribute, so that a review can see which rung a test case runs on.
- **The wording, level and tracing scripts aren't in CI,** because the framework isn't public. They run before each pull request instead, and CI runs `mellon-check` and the test suite.

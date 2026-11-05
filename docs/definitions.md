# Definitions

The terms Mellon's requirements and test cases use, and the only list of them. A term used in a requirement means exactly what it says here. When a requirement needs a new term, add it here in the same change. Where Mellon follows IBM DOORS, it uses DOORS's word.

## Repository and documents

| Term | Meaning |
|---|---|
| Repository | A git repository that holds documents in folders directly under its root. Mellon works only on the root folder of a complete clone, not a shallow one. |
| Document | A set of objects with one manifest, kept in a folder directly under the repository root. The folder's name is the document ID. |
| Document ID | The name of a document, made of letters, digits, hyphens and underscores. |
| Manifest | The file that holds a document's metadata, parent documents, attribute definitions and outline. |
| Metadata | A document's title, author and date. |
| Parent document | A document that another document declares it derives from. Coverage and the PDF traceability section are worked out against parent documents. |
| Load error | A document that Mellon can't read, reported with its file, line and reason. A document with a load error is still listed. |

## Objects

| Term | Meaning |
|---|---|
| Object | One entry in a document. It has an object ID, attribute values and a description, and can have a table, a figure and outgoing links. |
| Object ID | `<document ID>-<n>`, where `n` is a whole number. An object ID is never given to a second object in the same document. |
| Type | The attribute named `type`, which says what kind of object it is, such as `Heading` or `Requirement`. |
| Heading | An object whose type is `Heading`. Headings structure the outline, and coverage doesn't count them. |
| Description | An object's main text. On a heading it's the heading's title, and on a table it's the caption. |
| Outline | The order and nesting of a document's objects, as a tree. |
| Sub-object | An object directly below another object in the outline. |
| Attribute | A named value on an object, defined in the manifest with a kind and, for an enumeration, its options. |
| Kind | What values an attribute takes: enumeration, text, integer, real, boolean or date. |
| Required attribute | An attribute that every object must have a value for. |
| Table | Rows and cells kept on one object, with column alignment, header rows and merged cells. |
| Figure | A picture kept with one object, as an SVG file. A draw.io figure also carries the diagram that draw.io edits. |

## Links

| Term | Meaning |
|---|---|
| Link | A directed connection from one object, the source, to another object, the target, in the same document or another one. It has a link type, such as `verifies`. |
| Outgoing link | A link seen from its source. Only outgoing links are stored, with the source's document. |
| Incoming link | A link seen from its target, found by reading the whole repository. |
| Acknowledged commit | The commit of the target that a link was last reviewed against. |
| Suspect link | A link whose target object, or its figure, was changed by a commit after the link's acknowledged commit. |
| Broken link | A link whose target object doesn't exist. |
| Acknowledge | To record the target's latest commit as the link's acknowledged commit, after reviewing the change. |
| Trace | The chain of links from an object up through its targets, or down through its sources. |
| Coverage | The share of objects in one document that have a link to or from objects in another document. |

## Versions

| Term | Meaning |
|---|---|
| Commit | A git commit. |
| Latest commit | The commit at git's `HEAD`. |
| Working copy | The files on disk, including changes that aren't committed yet. |
| Uncommitted change | A file in a document folder that differs from the latest commit. |
| Issue | A numbered release of one document, recorded as the tag `<document ID>-issue-<n>` on a commit. Its number is one or more whole numbers separated by dots. |
| Current issue | The highest issue of a document reachable from the latest commit. |
| Tag | A named commit that marks a milestone for the whole repository, apart from the issue tags. |
| Version | One state of a document to compare: an issue, a branch, a commit or the working copy. |
| From, To | The two versions a comparison shows the changes between, from the older to the newer as the user chose them. |
| Added, Deleted, Modified, Moved, Link reviewed | The changes an object can have between two versions. **Moved** means its parent changed, or its order among the siblings it has at both versions changed: inserting or deleting a sibling moves nothing. **Link reviewed** means only the acknowledged commit of one or more of its outgoing links changed. |
| Merge in progress | A git merge that stopped because files conflict, and that hasn't been committed or aborted. |

## Checks

| Term | Meaning |
|---|---|
| Validation error | A problem in a document that loads: a missing required attribute, a value that doesn't fit its kind, a broken outline, or a rule that isn't met. Each kind of problem has a fixed code. |
| Rule | A condition in a document's rules file: objects that match a set of attribute values must have, or must not have, certain attributes or links. |
| View | A saved choice of which attribute columns the grid shows, and in what order. |
| Filter | A saved set of conditions on attributes and links that decides which objects the grid shows. |

## Programs and windows

| Term | Meaning |
|---|---|
| Mellon | The whole application: the GUI, the MCP server and `mellon-check`. |
| Project Explorer | The GUI's main window, which lists the documents in a repository. |
| Document Editor | The GUI window for one document. |
| Read-only mode | A Document Editor state in which no edit can be made. A window opens in it. |
| MCP server | The program that offers Mellon's functions to an agent over the Model Context Protocol, on standard input and output. |
| Read-only server | The MCP server started so that it refuses every tool that would change a file, commit or export. |
| Agent | A program that uses the MCP server, such as an AI assistant. |
| Released build | A Mellon build from a published release, as opposed to a development build. |
| Reference machine | The computer that performance requirements are measured on: the one `mellon/SCALING.md` records its measurements on. See open question Q2. |

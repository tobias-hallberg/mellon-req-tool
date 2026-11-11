# Definitions

The terms Mellon's requirements and test cases use, and the only list of them. A term used in a requirement means exactly what it says here. When a requirement needs a new term, add it here in the same change. Where Mellon follows IBM DOORS, it uses DOORS's word.

## Repository and documents

| Term | Meaning |
|---|---|
| Repository | A git repository that holds documents in folders directly under its root. Mellon works only on the root folder of a complete clone, not a shallow one. |
| Work tree | The folder tree that git checks a repository's files out into, with the repository's `.git` folder, or a `.git` file pointing to it, at the top. |
| Repository root | The top folder of a repository's work tree, which holds `.git` and the document folders. |
| Dot folder | A folder whose name starts with a dot, such as `.git`. No dot folder holds a document. |
| Document | A set of objects with one manifest, kept in a folder directly under the repository root. The folder's name is the document ID. |
| Document ID | The name of a document: one or more characters from A–Z, a–z, 0–9, hyphen and underscore, other than `CON`, `PRN`, `AUX`, `NUL`, `COM1` to `COM9` and `LPT1` to `LPT9` in any letter case. MELLON-STORE-SRS sets its greatest length. |
| Manifest | The file that holds a document's metadata, parent documents, attribute definitions and outline. |
| Metadata | A document's title, author and date. |
| Parent document | A document that another document declares it derives from. Coverage and the PDF traceability section are worked out against parent documents. |
| Data file | A file of a document that Mellon reads as XML: the manifest, an object file, a links file, a view file, a filter file or the rules file. A figure file isn't a data file. |
| Object file | The file `objects/<object ID>.xml` in a document's folder, which holds one object's attribute values, description and table. |
| Links file | The file `links/<object ID>.xml` in a document's folder, which holds one object's outgoing links. |
| Figure file | The file `figures/<object ID>.svg` in a document's folder, which holds one object's figure. |
| View file | The file `views/<slug>.xml` in a document's folder, which holds one view. |
| Filter file | The file `filters/<slug>.xml` in a document's folder, which holds one filter. |
| Slug | The name of a view file or filter file without `.xml`. A slug matches `[a-z0-9]+(-[a-z0-9]+)*`. |
| Load error | A report that a document can't be read, with the file, line and reason. A document with a load error is still listed. |
| On-disk format | The folders, files, elements and XML attributes that Mellon reads and writes in a repository. |
| Written form | The form in which Mellon writes a data file, as MELLON-STORE-SRS states it under Written form. |
| Canonical form | A file as Canonical XML 1.1 with comments writes it, with whitespace-only text between elements removed. Line endings, indentation, the order of XML attributes and the quote style don't show in it; comments do. |
| Stored text | Text that a user or agent gives Mellon to write into a data file. |
| Operation | One change to the working copy that a user makes with one action in the GUI, or an agent with one MCP tool call, together with each file the change writes or deletes. |
| Conflict markers | In one file, a line starting with `<<<<<<<`, a later line starting with `=======`, and a later line starting with `>>>>>>>`, as git writes them into a file whose changes it couldn't merge. |
| Partial clone | A clone that git was told to make without some of the history's files or folders, to fetch them when needed. |
| Sparse checkout | A working copy that git was told to fill with only some of the latest commit's folders. |
| Network path | A Windows path that starts with two slashes or two backslashes, other than a long local path (`\\?\C:\...`); `\\?\UNC\...` is a network path. |

## Objects

| Term | Meaning |
|---|---|
| Object | One entry in a document. It has an object ID, attribute values and a description, and can have a table, a figure and outgoing links. |
| Object ID | `<document ID>-<n>`, where `n` is a whole number. An object ID is never given to a second object in the same document. |
| Object number | The whole number `n` in an object ID `<document ID>-<n>`. |
| Next object number | The object number a document's manifest records for the next object to be created. A manifest that records none counts as recording 0. |
| Type | The attribute named `type`, which says what kind of object it is, such as `Heading` or `Requirement`. |
| Heading | An object whose type is `Heading`. Headings structure the outline, and coverage doesn't count them. |
| Description | An object's main text. On a heading it's the heading's title, and on a table it's the caption. |
| Outline | The order and nesting of a document's objects, as a tree. |
| Sub-object | An object directly below another object in the outline. |
| Parent object | The object that another object is a sub-object of. Not to be confused with a parent document. |
| Top level | The place in the outline of the objects that are no object's sub-object. |
| Sibling | An object with the same parent object as another object. Two objects at the top level are siblings too. |
| Ancestor object | An object's parent object, that object's parent object, and so on up to the top level. |
| Descendant object | An object's sub-objects, their sub-objects, and so on down the outline. |
| Attribute | A named value on an object, defined in the manifest with a kind and, for an enumeration, its options. |
| Label | The name an attribute is shown by: the label the manifest gives it; when it has none, `Type` for the `type` attribute, and its name for any other. |
| Kind | What values an attribute takes: enumeration, text, integer, real, boolean or date. |
| Option | One of the values an enumeration attribute allows, listed in order in the manifest. |
| Default value | A value given with a new attribute definition for Mellon to set on the document's existing objects. The manifest doesn't keep it. |
| Required attribute | An attribute that every object must have a value for. |
| Set, not set | An attribute is set on an object when the object's file gives it a value, even an empty one, and not set when the file doesn't mention it. |
| Blank | A value is blank when it is empty or holds only whitespace and characters with no width: the characters Unicode gives the White_Space property, and U+200B, U+200C, U+200D, U+2060 and U+FEFF. |
| Table | Rows and cells kept on one object, with column alignment, header rows and merged cells. |
| Header row | A table row marked as holding column headings. |
| Merged cell | A table cell that covers more than one row or column position. |
| Span | The number of columns (column span) or rows (row span) that a table cell covers. |
| Figure | A picture kept with one object, as an SVG file. A draw.io figure also carries the diagram that draw.io edits. |
| draw.io figure | A figure whose file also carries the diagram that draw.io desktop edits, as MELLON-STORE-SRS-132 tells a draw.io figure from another figure. |
| Field | One part of an object that a comparison looks at: an attribute value, the description, the table, the figure, or the outgoing links, each with its target and link type. |
| Word | In a description: a run of letters, digits and underscores, or one other character that isn't whitespace. So each punctuation mark is a word of its own, and `stop.` is the two words `stop` and `.`. Whitespace separates words and isn't one. |
| Invisible character | A character that Unicode gives the property Bidi_Control or Default_Ignorable_Code_Point. This takes in tag characters, zero-width characters and variation selectors. |

## Editing

| Term | Meaning |
|---|---|
| Edit | A request, from the user in the GUI or from an agent through an MCP write tool, that Mellon change a document's files. Mellon carries out an edit as one operation. Undo and Redo aren't edits. |
| Undo step | One operation that a Document Editor made on its document's files, with each file the operation wrote or deleted and that file's content before and after. |
| Undo history | The undo steps a Document Editor keeps, and the undone steps it can redo. |
| Filter condition | One test in a filter: an attribute, an operator and, for most operators, a value; or whether an object has outgoing links to, or incoming links from, a document. |
| Combinator | Whether a filter matches an object that meets each of its filter conditions (`all`) or at least one (`any`). |
| Active filter | The filter the grid applies at the moment, if any. |
| Active view | The view whose columns the grid shows at the moment, if any. |
| Hit | An object that the search_objects tool returns for a search. |

## Links

| Term | Meaning |
|---|---|
| Link | A directed connection from one object, the source, to another object, the target, in the same document or another one. It has a link type, such as `verifies`. |
| Outgoing link | A link seen from its source. Only outgoing links are stored, with the source's document. |
| Incoming link | A link seen from its target, found by reading the whole repository. |
| Link key | The source object ID and target object ID that name one link. |
| Acknowledged commit | The commit of the target that a link records as reviewed, set when the link is created or acknowledged. |
| Last change | The commit, found by going back from the latest commit, that last changed an object's file or figure in a way that counts as a change to the object. At a merge commit whose object file and figure equal one parent's, the search goes on through that parent only. An object whose file isn't at the latest commit has no last change. |
| Link state | One of unknown, broken, suspect and OK. Each link has exactly one: the first of the four that applies. A link's state is unknown when Mellon can't work it out, or hasn't yet. |
| Suspect link | A link whose target object exists in the working copy, and that has no acknowledged commit or one that isn't the target's last change. |
| Broken link | A link whose target object doesn't exist in the working copy. |
| OK link | A link whose target object exists in the working copy and at the latest commit, and whose acknowledged commit is the target's last change. |
| Acknowledge | To record the target's last change as a link's acknowledged commit, after reviewing the target as it is at that commit. |
| Trace | The objects reached from one object by following links up, source to target, or down, target to source, as a tree with that object at its root. |
| Depth | In a trace, the number of links between the traced object and another object, on the path by which the trace reaches it. The traced object's own targets and sources are at depth 1. |
| Coverage | The share of the counted objects of one document, the reported document, that links join to a second document, the covering document: incoming or outgoing by link direction, and direct through one link or transitive through a chain. |

## Versions

| Term | Meaning |
|---|---|
| Commit | A git commit. |
| Latest commit | The commit at git's `HEAD`. |
| Working copy | The files on disk, including changes that aren't committed yet. |
| Pending edit | Text typed in a Document Editor that Mellon hasn't written to the working copy yet. |
| Uncommitted change | A file in a document folder, other than an editor side file or a file git ignores, that differs from the latest commit, or that the latest commit holds and the working copy lacks. |
| Editor side file | A file whose name starts with `.$` or `~$`, for example the backup that draw.io leaves beside a figure it saves. It isn't part of a document. |
| Selected path | A file that Mellon is asked to commit: by the commit dialog, by `commit_changes`, or by a write tool's `commit_message`. A folder given as a path selects each file under the folder, in the working copy and at the latest commit. |
| Discard | To put a document's folder back as the latest commit holds the folder, throwing away the document's uncommitted changes. |
| Detached HEAD | git's HEAD naming a commit directly instead of a branch. |
| Issue | A numbered release of one document, recorded as the tag `<document ID>-issue-<n>` on a commit. Its number is one or more whole numbers separated by dots. |
| Issue tag | The tag `<document ID>-issue-<n>` that records an issue. |
| Raise the issue | To enter in the commit dialog an issue other than the prefilled one. |
| Upstream document | A document that holds the target of a link from one of the document's objects, or from an object of another upstream document. |
| Issue order | Issues are ordered by their numbers part by part, as whole numbers, so 2.9 comes before 2.10. When one issue's parts begin the other's, the shorter comes first, so 2 comes before 2.0. |
| Current issue | The highest issue of a document, in issue order, whose commit is the latest commit or an ancestor of it. |
| Issue before | For a given issue, the highest issue of the same document that comes before it in issue order and whose commit is the latest commit or an ancestor of it. |
| Branch | A git branch, local or remote-tracking. |
| Tag | A named commit that marks a milestone for the whole repository, apart from the issue tags. |
| Version | One state of a document to compare: an issue, a tag, a branch, a commit or the working copy. |
| Doesn't exist at a version | A document doesn't exist at a version when its folder is absent there. An object doesn't exist at a version when its object file is absent there. |
| Can't be read | A file can't be read, in the working copy or at a version, when it is there but can't be opened or read in full, or isn't a well-formed file of its kind: well-formed XML that meets each format rule whose breach MELLON-STORE-SRS makes a load error. A document's manifest also can't be read when the document's folder is there and the manifest isn't. |
| Changed since an issue | A document has changed since an issue when a file in its folder differs between the issue's commit and the latest commit. |
| From, To | The two versions a comparison shows the changes between. The changes are what it takes to go from From to To, whichever is older. |
| Unset | A From or To that names no version: nothing chosen, and nothing typed but whitespace. |
| From or To changes | A version is picked in it, or the text typed in it changes, whether or not Enter or Compare has been pressed since. |
| Added, Deleted, Modified, Moved, Link reviewed | The changes an object can have between From and To. **Added**: it exists at To and not at From. **Deleted**: it exists at From and not at To. **Modified**: it exists at both and a field differs. A difference only in a file's line endings, indentation or order of XML attributes is no change. **Moved**: it exists at both, and its parent changed, or it isn't in the largest set of its siblings at both versions whose order is the same at From and at To. Inserting or deleting a sibling moves nothing. When two such sets are equally large, the sibling that comes later at From is the one Moved. **Link reviewed**: its only change is to the acknowledged commits of its outgoing links. Moved can go with Modified or with Link reviewed. |
| Merge in progress | A git merge that has stopped before its merge commit, with or without conflicting files, and that hasn't been committed or aborted. |
| Git operation in progress | A merge, rebase, cherry-pick or revert that git stopped and that hasn't been finished or aborted. |
| Merge commit | A commit with more than one parent. |
| Ours | In a merge in progress, the side at the latest commit: the branch being merged into. |
| Theirs | In a merge in progress, the side at the commit being merged in. |
| Merge base | A common ancestor of ours and theirs that isn't an ancestor of another common ancestor. After merges both ways two sides can have more than one; which one Mellon merges from is in MELLON-VER-SRS. |
| Conflicting file | A file that the git index holds as unmerged, after a merge, rebase, cherry-pick, revert, `git am` or stash stopped on the file, until the file is marked resolved. |
| Resolve | To write the merged document files of a merge in progress and mark them resolved in the git index. |
| Creating commit | For an object new on one side of a merge, the commit between the merge base and that side that first added the object's file. |
| Carry the same change | Two commits carry the same change when each makes the same changes to the same files relative to its own parent, as a commit and its cherry-pick do. |
| Renumber | To give an object a new object ID while resolving a merge, because both sides created different objects under one object ID. |

## Checks

| Term | Meaning |
|---|---|
| Validation error | A problem that validation finds in a document that loads: in its manifest, rules file, outline, objects, figures or links. Each kind of problem has a fixed code. |
| Check problem | A load error, a validation error or a suspect link: the three kinds of problem that `mellon-check` reports and fails on. A broken link is reported as a validation error. |
| Rules file | The file `<document ID>.rules.xml` beside a document's manifest, which holds the document's rules. A document needn't have one. |
| Rule | A rule condition and its rule directives, in a document's rules file. |
| Rule condition | The part of a rule that says which objects the rule applies to: attribute values, and whether an object has outgoing links. A rule condition can name nothing. |
| Rule directive | The part of a rule that says what an object it applies to must have: an attribute required, optional or forbidden, or an outgoing link to a named document required. |
| View | A saved choice of which attribute columns the grid shows, and in what order. |
| Filter | A saved set of conditions on attributes and links that decides which objects the grid shows. |

## Exporting

| Term | Meaning |
|---|---|
| Export | Writing one document to a PDF file, with the Document Editor's Export action or the MCP server's export tool. |
| Export action | The Document Editor action that exports the open document. |
| Exported PDF | The PDF file that one export writes. |
| Output path | The file path that an export writes the exported PDF to. |
| Export start | For the Export action, the moment the user confirms the output path; for the MCP server's export tool, the moment the MCP server receives the call. |
| Export result | What Mellon reports when an export ends: for the Export action, what the Document Editor shows; for the MCP server's export tool, the tool call's result. |
| Export fails | An export fails when it ends without writing the exported PDF for a reason other than the user cancelling the export. |
| Exported version | The version an export reads the document at: the working copy, unless the export chooses another version. |
| Exported commit | The exported version's commit, or the latest commit when the exported version is the working copy. |
| Issue at a version | For a version, the highest issue of a document, in issue order, whose commit is the version's commit or an ancestor of it. At the working copy it is the current issue. |
| Uncommitted document file | A file in a document's folder that the on-disk format defines and that differs from the latest commit. |
| Cover page | The first page of an exported PDF. |
| Revision label | The text that the MCP server's export tool is given, as `rev`, to show as the document's revision. |
| Traceability section | The last section of the exported PDF of a document with parent documents, which states its coverage against them and lists its links to their objects. |
| Export warning | The Document Editor's warning, before the save dialog of an export, about the document's outgoing links whose link state is suspect, broken or unknown, its uncommitted changes and its validation errors. |
| Figure program | The program Mellon draws figures with for an export. Mellon 0.3.0 uses draw.io desktop. |
| Figure page | One page of a draw.io figure's diagram. The shown figure page is the one whose picture the figure file holds. |
| Figure placeholder | The line an exported PDF shows in place of a figure that the PDF doesn't draw. |
| Notice of figures not drawn | The Document Editor's message, at the end of an export, that lists the objects whose figures the exported PDF doesn't draw. |
| Printable character | A character from U+0021 to U+007E, or of the Unicode blocks Latin-1 Supplement, Latin Extended-A, General Punctuation or Greek and Coptic, or ≤ or ≥, other than control characters, whitespace and invisible characters. |

## Programs and windows

| Term | Meaning |
|---|---|
| Mellon | The whole application: the GUI, the MCP server and `mellon-check`. |
| Project Explorer | The GUI's main window, which lists the documents in a repository. |
| Document Editor | The GUI window for one document. |
| Grid | The Document Editor pane that lists the document's objects, one row each, with a column for each attribute shown. |
| Outline pane | The Document Editor's left pane, which shows the outline as a tree. |
| Selected object | The object selected in a Document Editor's outline pane and grid. |
| Read-only banner | The banner above the grid shown while a Document Editor is in read-only mode. |
| Status bar | The line at the bottom of the Project Explorer and of each Document Editor. |
| State gutter | The grid column that shows a mark for the states of each object's outgoing links. |
| Inspector | The Document Editor tab that shows the selected object's attributes, description, outgoing links and incoming links. |
| Suspect banner | The bar above the grid that holds the Suspects only and Ack selected controls. |
| Link picker | The Document Editor dialog in which the user chooses a target object and a link type to add an outgoing link. |
| Trace view | The Document Editor dialog that shows the trace of the selected object. |
| History tab | The Document Editor tab that lists the commits that changed the selected object. |
| Compare dialog | The GUI window that shows the changes between two versions of one document. |
| Commit dialog | The dialog, opened from a Document Editor, that commits the open document's folder, with the commit message, the issue, the document's author and date, and an optional tag name. |
| Resolve merge dialog | The GUI window in which the user resolves a merge in progress. |
| Merge banner | The banner that says a merge is in progress. |
| Changed on disk banner | The Document Editor banner shown when the document or one of its parent documents changed on disk outside the window. |
| Properties dialog | The Document Editor dialog that edits the document's metadata, parent documents and attribute definitions. |
| Advanced filter dialog | The Document Editor dialog that creates and edits a filter. |
| Filter box | The text box above the grid in which the user types text to find objects by. |
| Table editor | The Document Editor's in-place editor for one object's table, opened from the table shown in the grid. |
| Table menu, Figure menu, Filters menu, Views menu | The Document Editor toolbar menus that hold the actions on the selected object's table, the actions on its figure, the document's filters and the document's views. |
| draw.io desktop | The draw.io program for Windows, macOS and Linux (https://www.drawio.com/), which Mellon opens figures in and renders figures with. It isn't part of Mellon. |
| Read-only mode | A Document Editor state in which the window makes no edit from the user's input. Other programs, such as an agent, can still change the files. |
| Action | A command that the user starts in a GUI window: from a menu, a toolbar, a button, a context menu, a shortcut, a drag and drop or a paste. Keys that a text field handles itself (Esc, Enter, Ctrl+Enter and the keys that edit text) don't start an action. |
| Text field | A control in a GUI window that the user types text into. |
| Activated, active | A window is active while it receives keyboard input, and is activated when it becomes active. |
| Shown | A window is shown while it is open and not minimised, whether or not it is active. |
| Reload | To read a document and its parent documents again from the working copy, in place of the copy that a Document Editor read before. |
| Changed on disk | A file in a document folder has changed on disk for a Document Editor when the file was added or removed, or holds content other than what the Document Editor last read or wrote there. |
| Theme | The set of colours the GUI draws with: Light or Dark. |
| Unexpected error | An error in the GUI or the MCP server that Mellon has no planned response for: not a refusal, a load error or a validation error. |
| Fatal error | An error that ends a program at once, outside its handling of unexpected errors, for example an access violation. |
| Error log | The file that the GUI appends unexpected errors to. |
| `mellon-check` | The command-line program that checks a repository for check problems and reports the result by its exit status. |
| Validate action | The Document Editor action that validates the open document and shows the result. |
| MCP server | The program that offers Mellon's functions to an agent over the Model Context Protocol, on standard input and output. |
| Read-only server | The MCP server started so that it refuses every tool that would change a file, commit or export. |
| Agent | A program that uses the MCP server, such as an AI assistant. |
| Tool | A named operation, with named parameters, that the MCP server offers an agent under the Model Context Protocol. |
| Tool call | One request from an agent to run one tool, and the result the MCP server returns for it. |
| Write tool | A tool that the table of MELLON-MCP-SRS-35 names. |
| Folder to serve | The folder that MELLON-MCP-SRS-3 to -7 choose when the MCP server starts. |
| Instructions | The text the MCP server returns when an agent connects, in the Model Context Protocol's `instructions` field, which the agent's client can pass to the agent. |
| Log level | How much the MCP server writes to standard error: `DEBUG`, `INFO`, `WARNING` or `ERROR`, from the most messages to the fewest. Each log message has one of these levels, and each level is more severe than the ones before it. |
| Trailer | A `Key: value` line in the last paragraph of a commit message, as git defines trailers. |
| Released build | A Mellon build from a published release, as opposed to a development build. |
| Release number | The number of a Mellon build, in the form 0.3.0. |
| Windows installer | The program `Mellon-Setup-<version>.exe`, which installs Mellon on Windows. |
| Install folder | The folder that the Windows installer installs Mellon into. |
| Source checkout | A clone of Mellon's own source code, run without the Windows installer. |
| Reference machine | The computer that performance requirements are measured on: the one `mellon/SCALING.md` records its measurements on. See open question Q2. |
| Reference document | A document made for a performance requirement as `mellon/SCALING.md`'s method makes one: of the stated number of objects, each object other than a heading linking to one object of a parent document of the same size, with as many commits as objects in its history. |

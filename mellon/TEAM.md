# Working on a Mellon repository as a team

Mellon stores requirements as XML files in a git repository. Mellon itself only
**commits** — pulling, pushing and resolving conflicts happen in your normal git
client. This page is the whole routine; it is short on purpose.

## One-time setup

1. **Install Git for Windows** (<https://git-scm.com/download/win>) and one git
   client you are comfortable with (GitHub Desktop, TortoiseGit, Fork, or the
   command line).
2. **Set your name and e-mail** — Mellon refuses to commit without them:

   ```
   git config --global user.name  "Your Name"
   git config --global user.email "you@example.com"
   ```

3. **Clone the team's requirements repository to a local disk** (e.g.
   `C:\work\<project>-reqs`). Avoid OneDrive folders and network drives — git
   may refuse repositories it doesn't consider owned by you.
4. **Open the clone in Mellon** (**Menu → Open Repository…**, `Ctrl+O`).
   Open the folder that *contains* the document folders — the root of the
   clone.

The person who creates the team repository commits a `.gitattributes` file at
its root once, so line endings stay consistent across machines:

```
* text=auto eol=lf
```

and a `.gitignore` with draw.io's backup files, which draw.io leaves beside
every figure it saves (Mellon never commits them, but your git client would
list them as changes):

```
.$*
```

## Daily loop

1. **Pull** in your git client.
2. **F5** in every open Mellon window: *Refresh* in the Project Explorer,
   *Reload from disk* in each document window. While the amber *Changed on
   disk* banner shows, Mellon refuses edits until you reload.
3. **Edit** in Mellon. Document windows open read-only; click **Enable
   editing** in the banner (they return to read-only after each commit).
4. **Commit** in Mellon (`Ctrl+S`). Leave the issue field as it is for a normal
   commit; raise it only when you deliberately cut a new issue of the document.
5. Check that your git client shows **0 changes** — Mellon commits one document
   folder at a time, so edits in other documents stay behind: another
   document window, or links in other documents removed when you deleted an
   object they pointed at. Commit those as well before pushing.
6. **Push.** If you cut a new issue or named a tag, also push the tags:
   `git push --tags` (a plain `git push` doesn't push tags).

## Rules that prevent most conflicts

- **One writer per document at a time.** Agree who is editing which document
  (or which section of a large one).
- **Adding objects on several branches is fine.** New objects get the next free
  number on your branch, so two branches can both create, say, `DOC-51`.
  Mellon sorts that out when the branches are merged (below): nothing is lost.
- **Merge commits only.** Use `git pull` with merge (Git for Windows' default),
  and don't squash or rebase merge requests on the server. Squash and rebase
  rewrite the commits that suspect-link acks point at, so every acked link to a
  changed object turns suspect again.

## When a pull reports a conflict

1. Leave the merge as it is (don't resolve document files in your git client)
   and switch to Mellon. The Project Explorer shows **Merge in progress** with
   a **Resolve merge…** button (press F5 if it isn't there yet).
2. In **Resolve merge…**:
   - **Same id created on both branches:** nothing to do. The requirement
     committed later gets the next free id, together with its links, figure,
     place in the outline and the links its branch made to it.
   - **Same requirement changed on both branches:** different fields merge by
     themselves. A field both changed is yours to merge: write the merged
     text (prefilled with yours), or pick a value. The requirements it traces
     to are shown above it.
   - Link files and the outline merge by themselves.
3. Press **Resolve**; every choice has a default. Conflicts outside document
   folders are left for your git client.
4. Make the merge commit in your git client (Mellon won't commit while a merge
   is in progress), then **F5** and **Validate** in Mellon. Links to a
   renumbered object show as suspect until acked.

## Undoing your own edits

Every edit is written to disk straight away. **Ctrl+Z** in the document's
window undoes your edits one at a time, back to the last commit (**Ctrl+Y**
redoes). The history clears when you commit or press F5, and when someone
else's change lands on the files. To throw away everything you changed in a
document since its last commit at once (including objects you created), use
**Menu → Discard uncommitted changes to this document…** in that document's
window.

## Suspect links after someone else's changes

When a parent requirement changes, links pointing at it turn **suspect**. Review
each one (the parent object's **History** tab shows what changed), update the
child if needed, **Ack** it, and commit. Revising a parent and re-acking its
children always takes two commits — that's by design.

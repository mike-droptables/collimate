---
name: modify-files
description: Edit the cell's files to a described task using the file tools. One cell is edited in place; a declaration of many candidates produces options, one edit per candidate. Use to apply the same change across files or across rival candidates.
---
# Modify Files

Apply the task in the **run prompt** to the cell's files, using the file tools
(`list_files`, `read_file`, `write_file`, `edit`). Dispatch on the shape of the
input.

## 1. Read the input and choose the dispatch

Read the selected `.options_stack` first. Its named options are distinct
context sources; ordinary folders are structure, not options. Read the matched
files for each option and retain its name in your notes. Without a declaration,
read the workspace as one body of context.

With options, apply the task independently to each selected candidate and retain its files and label. Write the completed candidates to separate output folders and replace the root declaration last, selecting those edited folders. With one ordinary input, edit in place.

## 2. Edit

For each target, use the file tools to make exactly the change the task
describes:

- **Read before you write.** Understand the file, then make the minimal correct
  edit.
- **Propagate deletions** — if the task means a file should go, delete it; don't
  leave a stale copy.
- If the task was really a question, answer it in your reply rather than
  silently changing nothing.

## 3. Markdown drafts the user should read

A markdown file you write as a **draft for the user** — a proposal, a plan, a
summary — is just an ordinary cell file: write it plainly and let the user read
and edit it in the editor. If your reply would answer the task in prose rather
than by editing, answer in your reply.

## 4. Multiple candidates

Apply the task independently inside each candidate's folder. Preserve each
candidate's files and label. Write `index.md` with the task and one status line
per candidate, clearly identifying any failed edit.

## 5. Finish

End your reply with what changed: which files (or which candidates).

## Options file format

Read the selected declaration with `gen.options()` or the file tools: root first,
then the fewest folder levels, then lexical path order. Without a declaration,
the workspace is one body of input; ordinary folders do not identify options.
Use each option's `name` as its label and read the files its globs select.

```yaml
options:
  - name: First candidate
    root: candidates/first
    files: ["**"]
  - name: Second candidate
    root: candidates/second
    files: ["**"]
rest: keep
```

Each option needs a nonempty `name` and a nonempty `files` list. `root` is an
optional literal folder, never a glob. Globs are relative to `root` when set,
otherwise to the declaration's folder; only `*`, `**`, and `?` are special.
Other characters are literal. Options may overlap. Files inside `root` lose
that prefix when split; matched files outside it keep paths relative to the
declaration's folder. The consumed declaration is excluded from every option.
Check that roots exist, each option selects real files, paths stay in the cell,
and mapped paths do not collide. Use available file tools for these checks.

`rest: keep` leaves unselected files in the source; `drop` removes the source;
`each` copies unselected files into every option and removes the source.
Live surfaces (the run folders) remain in the source. Keep
reports and evidence at the root, outside candidate folders, with `rest: keep`.
After writing all candidate files, write the root `.options_stack` last with
`update_file`; wait for the final acknowledged push before claiming success.
The user presses Split to get option cells. Work only in this run's cell.

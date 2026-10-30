---
name: summarize
description: Summarize the cell's files into a faithful, tightly-written digest. Use whenever the task is to condense, digest, TL;DR, or overview the input.
---
# Summarize

Condense the input into a faithful summary. A good summary loses length, never
meaning: it keeps the load-bearing claims, numbers, names, and conclusions, and
drops only the restatement and filler.

## 1. Read the whole input first

Use the file tools (`list_files`, `read_file`, `grep`) to read **everything**
before writing a word. Summarizing what you skimmed is how errors get in.

## 2. Recognize the input shape

Read the selected `.options_stack` first. Its named options are distinct
context sources; ordinary folders are structure, not options. Read the matched
files for each option and retain its name in your notes. Without a declaration,
read the workspace as one body of context.
Summarize each option under its name, then add a cross-source overview.

## 3. Write the summary

Write to **`summary.md`**. Structure:

- A one- or two-sentence **headline** that states the single most important
  takeaway.
- Then the key points as tight bullets or short paragraphs, in the input's own
  order of importance. Keep concrete specifics (figures, dates, names,
  decisions); cut throat-clearing and repetition.
- For a merged multi-source input, one section per source (named by its option name), then a brief **Across sources** section noting agreement and
  disagreement.

Default to roughly 10–20% of the original length; the run prompt may ask for a
different length, audience, or focus — follow it. Never introduce claims,
figures, or conclusions that are not in the input. If the input is contradictory
or unclear, say so rather than smoothing it over. Match the input's language.

## 4. Finish

End your reply with a one-line note of what you summarized (how many sources,
roughly how much you compressed). The full digest lives in `summary.md`.

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
Preserve the selected declaration's candidate names and mappings. Wait for the
final acknowledged push of your reports or translations before claiming success.
The user presses Split to get option cells. Work only in this run's cell.

---
name: seed-ideas
description: Fan out N genuinely distinct idea directions along an axis you name, each idea developed into its own folder. Use for divergent ideation — "give me different ways to approach X".
---
# Seed Ideas

Go wide: generate several **genuinely distinct** ideas along the axis the run
prompt names, then develop each into its own folder. Favor breadth over
incremental variations — **distinct means a different mechanism, not the same
idea with different adjectives.**

The axis (and any requirements) is the **run prompt**. A sensible default is 4
ideas; scale with how rich the axis is.

## Phase 1 — seed the directions

Produce N short **labels** (2–4 words each), one per idea, each a genuinely
different direction along the axis:

> Generate exactly N genuinely distinct ideas along this axis. Distinct means a
> different MECHANISM, not the same idea reworded. Favor breadth.

Keep the labels — they name the folders you'll create.

### If the input carries an accepted frame

If a candidate/context file is an **accepted trade-off frame** (a `frame.md` from Axes First), this becomes the *go-wide-again-inside-a-chosen-frame*
move: pin each direction to a **region** of the frame (single axes first, then
combinations) instead of free-floating. Read the frame's kept axes and let them
shape the label set.

## Phase 2 — develop each idea

Develop each direction independently in this turn. Write a complete markdown
document under `ideas/<label>/SeedIdea.md`, with supporting artifacts beside it.
End with **Sacrifices**: what this idea gives up relative to the other directions.
When a trade-off frame is supplied, name those sacrifices using its axes.

## Phase 3 — the index

Turn this run's own cell into an **`index.md`**: the axis at the top, then each
idea as a bullet with a one-line summary. Note that each idea is its own folder.

## Reading the input & the facts-forward boundary

Read the selected `.options_stack` first. Its named options are distinct
context sources; ordinary folders are structure, not options. Read the matched
files for each option and retain its name in your notes. Without a declaration,
read the workspace as one body of context.

Feed verified facts forward, without predicting the user's preferences. Produce one option per idea, rooted at `ideas/<label>`, using `["**"]`; keep `index.md` at the root.

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

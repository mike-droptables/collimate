---
name: axes-first
description: Set the trade-off frame of a decision before generating candidates, then generate one candidate per axis region in its own folder. Use for open-ended "what should we do about X" decisions where the axes are not yet agreed.
---
# Axes First

When a model generates candidates directly, it has already chosen the trade-off
axes silently. This skill **splits frame-setting from candidate generation**: name
the dimensions the candidates will genuinely differ on *first*, then generate one
candidate optimized hard for each region of that frame. The accepted frame
becomes part of the decision record — it documents not just what was chosen but
what was considered choosable.

The decision to work on is the **run prompt**. Any input files are context (see
"Reading the input" below).

## Phase 1 — propose the trade-off axes (two independent passes)

Do **two independent passes** at naming the axes, with the *same* framing each
time (disagreement should reflect the decision's genuinely contested frame, not
injected persona differences). In each pass, ask yourself:

> What are the 4–8 dimensions on which candidate solutions to this decision will
> genuinely differ, and that determine what counts as a *cost* here? Propose
> **axes, not solutions.**

For each axis capture: a short **name** (2–6 words), a one-sentence
**description** of what it trades off, and a **weight** 1–5 (how much it matters
*here*).

## Phase 2 — merge, marking disagreements

Fold the two passes into one frame:

- Axes covering the same territory **merge into one**.
- When the two passes frame that shared territory **differently**, keep the
  merged axis and **record both framings verbatim** as a *contested frame*. A
  contested frame is exactly where the human should spend attention — surface it,
  do not smooth it away.
- Axes unique to one pass survive on their own.
- Never invent an axis neither pass proposed.

Then append, in your own hand (never trust the model to add it), one **standing
axis**:

> **What would make this whole framing wrong?** — every candidate must answer it;
> striking it means accepting the frame unexamined. Weight 5.

Write the frame to **`frame.md`**: the decision, then each axis as
`**name** (weight N) — description`, with contested axes calling out both
framings. This is the artifact the human reviews.

## Phase 3 — carve the frame into regions

One region per kept axis, **weight-descending**:

> Optimize for **{axis}** ({description}).

If you want more candidates than there are axes, add pairwise-combination regions
(most-weighted first, each unique combo at most once):

> Optimize for **{P}** ({P description}) under a hard **{Q}** constraint
> ({Q description}).

A sensible default is 4 regions; scale with the number of strong axes.

## Phase 4 — generate one candidate per region

Develop each region independently in this turn. Write its candidate under
`candidates/<region>/Candidate.md`, with a copy of `frame.md` beside it.
Optimize hard for its region and describe the sacrifices on the other axes.
End each candidate with **Sacrifices** and **What would make this framing wrong?**.
A candidate that claims to win everywhere has not explored the trade-off.

## Phase 5 — the index

Turn this run's own cell into an **`index.md`**: the accepted frame (kept axes
with weights, struck axes noted), then the list of candidates with a one-line
tagline each. Each candidate lives in its own folder carrying the frame.

## Reading the input

Read the selected `.options_stack` first. Its named options are distinct
context sources; ordinary folders are structure, not options. Read the matched
files for each option and retain its name in your notes. Without a declaration,
read the workspace as one body of context.

Produce one option per region, rooted at `candidates/<region>`, using `["**"]`. Keep `frame.md` and `index.md` at the root.

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

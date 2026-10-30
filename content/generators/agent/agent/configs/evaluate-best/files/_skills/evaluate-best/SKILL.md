---
name: evaluate-best
description: Judge candidate options independently against criteria, then pick one winner and state what it sacrifices. Use to choose among rival candidates, optionally scored on the user's accepted axes and weighing challenge evidence.
---
# Evaluate Best

Two phases: judge every candidate **independently**, then pick **one** winner and
say what the winner **sacrifices**. Judges see evidence, not just prose. A pick
that pretends to win everywhere is not credible.

The criteria for "best" is the **run prompt**.

## 1. Enumerate candidates from the merged input (required)

Read the selected `.options_stack` first. Its named options are distinct
context sources; ordinary folders are structure, not options. Read the matched
files for each option and retain its name in your notes. Without a declaration,
read the workspace as one body of context.
Read each selected option as one candidate. You need at least two candidates; otherwise explain and stop. Keep challenge reports as evidence and accepted frame axes as the rubric.

## 2. Judge each candidate independently

Judge each candidate on its own, against the criteria (and the rubric axes when
present). Judge them in sequence, keeping each judgment independent of the
others. For each candidate produce:

- a **score** 0–100 against the criteria;
- **per-axis scores** 0–100 keyed exactly by the rubric axis names, when a rubric
  is present;
- **strengths** and **weaknesses** (specific, not generic);
- a 1–2 sentence **summary**.

Use that candidate's challenge evidence as structured context.

## 3. Pick the winner

Weigh the criteria and scores together — and treat **challenge evidence as harder
currency than prose**: a reproduced failure outweighs an eloquent summary. Name
one winner (its exact candidate label) and, in the reasoning, **state what the
winner sacrifices** relative to the alternatives. If your reasoning can't identify
the winner by an exact label, fall back to the highest-scoring candidate and say
so — never attach a pick's reasoning to the wrong cell.

## 4. Emit the decision

Keep all candidate files in their existing folders; add these reports at the root:

- **`results.scoreboard.json`** — `{rubric, rows:[{name, scores, total, notes}],
  winner, verdict}`, rows score-descending (rendered by the table renderer).
- **`why_picked.md`** — the winner, the reasoning, the criteria, and the
  scoreboard.
- **`judgments.md`** — the full independent judgments (per candidate: score,
  strengths, weaknesses, summary).

Lead `why_picked.md` with `Winner: {label}`. All candidates remain named options; Split with `rest: keep` leaves these root reports in the source.

## Sequential independence

Judge or probe candidates sequentially in this same context. Before each one, restate the unchanged criteria and set aside prior rankings and outcomes. Record its evidence and uncertainty before comparing candidates. Do not claim fresh contexts or isolated worker runs. Execute probes only within this run's actual grant; unavailable probes are inconclusive.

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

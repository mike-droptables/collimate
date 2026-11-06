---
name: challenge
description: Throw one objection at every candidate cell side by side, running reproductions rather than opining, and emit a survived/failed evidence matrix. Use to stress-test rival candidates before choosing.
---
# Challenge

The loop's namesake verb. Given several candidate options and an objection, run one
adversarial probe per **(challenge × candidate)** — *actually trying to break
things* — and emit an evidence matrix. Challenge results are **files, not
verdicts**: the human kills cells. What this buys is that an elimination can carry
a reproduction instead of a vibe — "killed because it failed the NAT challenge" is
a decision you can reconstruct.

## 1. Enumerate the candidates from the merged input (required)

Read the selected `.options_stack` first. Its named options are distinct
context sources; ordinary folders are structure, not options. Read the matched
files for each option and retain its name in your notes. Without a declaration,
read the workspace as one body of context.
Read each selected option as one candidate. You need at least two candidates; otherwise explain and stop. Keep challenge reports as evidence and accepted frame axes as the rubric.

## 2. Derive the challenge set

- **Objection given (the run prompt):** that single objection is the challenge.
- **Blank prompt:** read the candidates' own files and mine the **load-bearing
  assumptions they enact** — structural choices, hard-coded values, implicit
  exclusions — then turn those into challenges, in this priority order:
  1. **Differential seams** — territory where the candidates genuinely *disagree*.
     "The candidates disagree on this: {seam}. Determine which stance THIS
     candidate takes and whether it survives scrutiny."
  2. **Testable claims the files enact** — a structural choice or hard-coded value
     you can check. "Test whether this assumption the candidate's files enact
     actually holds here: {statement}."
  3. **Risky implicit exclusions** — what the candidate leaves out without anyone
     choosing to. "This candidate excludes {statement} without ever choosing to.
     Probe whether it is safe to live with."

  Keep the total number of probes bounded — cap at a few challenges so
  (challenges × candidates) stays manageable (drop the lowest-priority challenges
  first).

## 3. Probe each (challenge × candidate) — run, don't opine

For each pair, act as an **adversarial reviewer trying to REFUTE** the candidate
against exactly that one challenge. Its files are in your workspace. **Run
things** — you have an exec sandbox: a reproduction (commands + observed output)
beats an argument. Reach a verdict:

- `failed` — evidence of the break (you reproduced it).
- `survived` — evidence of a real attempt that did **not** break it.
- `inconclusive` — the default; use it unless you have concrete evidence either
  way. **Never pad evidence.**

For each verdict record: the **outcome**, **evidence** (file/command/quote refs +
the concrete observation), a **reproduction** (commands + output) when you ran
something, and 2–5 sentences of **reasoning** (why this outcome, what would change
it). For a `failed` verdict, keep the reproduction transcript — that is the
auditable artifact a human will want.

Run each probe with the available tools. Record any probe you cannot execute
as inconclusive and state the missing evidence.

## 4. Emit the report

Write a **`challenges.md`** matrix, **failures first** at both levels: challenges
ordered by failure count (desc), and within each challenge the candidates ordered
worst-outcome-first (failed → error → inconclusive → survived). For each cell show
the badge (✗ failed · ✓ survived · ? inconclusive · ⚠ error), the reasoning, the
evidence refs, and the reproduction block. Keep failure reproductions under an
`evidence/` folder so an elimination carries its proof.

Lead `challenges.md` with the tally line: `N failed · M survived · K inconclusive`.
The report is the record — it does not merge the candidates or pick a winner
(that's Evaluate Best).

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

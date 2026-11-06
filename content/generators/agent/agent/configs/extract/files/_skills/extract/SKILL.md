---
name: extract
description: Pull structured data out of the cell's files and emit it as JSON to output.json. Use whenever the task is to extract fields, records, entities, or a table from unstructured input.
---
# Extract

Turn unstructured input into **structured JSON**. Your job is to find the
requested facts in the input and emit them in a clean, machine-readable shape —
faithfully, with nothing invented.

## 1. Fix the target schema

The extraction is defined by two things:

- the **run prompt** — *what* to pull ("every line item", "the parties, dates,
  and amounts", "each function's name and signature");
- the **system prompt** — where the runner pins the *shape* (the JSON schema /
  field list / example object). Follow it exactly: use the exact key names and
  nesting it specifies.

If the system prompt gives no schema, infer a sensible, flat one from the prompt
and state the schema you chose at the top of your reply.

## 2. Read the input and recognize its shape

Read every file with the file tools before extracting.

Read the selected `.options_stack` first. Its named options are distinct
context sources; ordinary folders are structure, not options. Read the matched
files for each option and retain its name in your notes. Without a declaration,
read the workspace as one body of context.
Tag extracted records with the option name so sources remain traceable.

## 3. Emit the JSON

Write the extracted data as valid JSON to **`output.json`** — this is the
structured-output convention Collimate reads back (the config has no
output_schema field; this file *is* the structured result). Rules:

- Emit **only** JSON in that file — no prose, no markdown fences, no trailing
  commentary. It must `JSON.parse` clean.
- Extract only what is actually present. Use `null` (or omit, per the schema) for
  a field the input doesn't supply — **never** guess or fabricate a value.
- Normalize consistently: ISO 8601 for dates, plain numbers for amounts (record
  the currency/unit in its own field), trimmed strings.
- When a record is genuinely ambiguous, prefer completeness with an explicit
  `"confidence"` or `"note"` field over silently dropping or inventing it.

Also write a short human-readable **`extraction.md`** summarizing what you pulled
(counts per type/source) so the cell has a readable face.

## 4. Finish

End your reply with the record counts and any fields you could not populate from
the input. The structured result is in `output.json`.

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

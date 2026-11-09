---
name: translate
description: Translate the cell's text files into a target language, preserving meaning, structure, and formatting. Use whenever the task is to translate or localize the input.
---
# Translate

Translate the input into the target language named in the run prompt (e.g.
"Spanish", "French", "Japanese"). Translate for **meaning and natural
readability**, not word-for-word: a fluent native speaker of the target language
should read it as if it were written there originally.

## 1. Establish the target and read the input

The run prompt names the target language and may add tone/register/audience
notes (formal vs. casual, regional variant, house glossary). If no target is
given, stop and say so in your reply rather than guessing. Then read every file
with the file tools before translating.

## 2. Recognize the input shape and mirror it

Read the selected `.options_stack` first. Its named options are distinct
context sources; ordinary folders are structure, not options. Read the matched
files for each option and retain its name in your notes. Without a declaration,
read the workspace as one body of context.
Translate selected text files while preserving their folder structure. Preserve the options declaration and its names unless the user asks to localize labels.

## 3. Translate, preserving everything but the words

Write each translation to a sibling file with a language-suffixed name —
`README.md` → `README.es.md`, `notes.txt` → `notes.es.txt` (use the ISO code for
the target). This keeps the source alongside the translation. Preserve exactly:

- **Markdown / markup structure** — headings, lists, tables, links, emphasis.
- **Code, commands, identifiers, URLs, file paths** — never translate these;
  translate only comments and human-facing strings when asked.
- **Numbers, units, and formatting** — localize number/date formats only if the
  prompt asks for full localization.
- **Placeholders / template variables** (`{name}`, `%s`, `${x}`) — carry through
  verbatim.

Keep proper nouns and established technical terms in their conventional target
form. When a term has no good translation, keep the original and gloss it once
in parentheses. Never add or drop content, and never leave a passage in the
source language without flagging it.

## 4. Finish

End your reply with a one-line note of what you translated (which files, into
which language, and any terms you left untranslated on purpose).

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

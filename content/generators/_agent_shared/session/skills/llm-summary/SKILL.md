---
name: llm-summary
description: Write a .llm_summary file when the user asks for a summary or a handoff of the work, and read one in full first when asked to pick up or continue from it.
---
# LLM summary

A `.llm_summary` file is a markdown summary of a piece of work, written so
a later session — yours or another run's, on any surface — can pick the
work up from it. The cell shows it as markdown. It is written only when the
user asks for a summary or a handoff; nothing writes one automatically.

## Writing one

Write `<name>.llm_summary` at the cell root (never under `_runs/`), where
`<name>` is a short kebab-case name for the work (`q3-report.llm_summary`).
For the same work, update the same file rather than starting another.

It must stand alone — the next reader has none of this conversation:

1. **Goal and plan** — what the work is for and how it is being done.
2. **Done** — what is finished, with links to the evidence (the files,
   the results).
3. **Not done** — what is left, and why.
4. **Decisions, surprises and gotchas** — what was chosen and why, and what
   would trip up the next session.
5. **Open requests** — what the user asked for that is still open, quoted as
   written. On the Annotation surface: the open threads, each with its id.
6. **Next steps** — in order.

Plain markdown with cell-relative links. Then tell the user where it is.

## Reading one

When asked to pick up or continue from a `.llm_summary` file, read the
whole file first and treat it as the state of the work. Answer with where
things stand and what comes next, then carry on as asked. Never delete or
rename it.

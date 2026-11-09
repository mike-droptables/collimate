---
name: annotations
description: How this run talks with the user on the Annotation surface — the messages each turn carries, how to reply (annotate, list_annotations), what context items and linked files mean, and how to answer visually with an HTML file.
---
# Annotations

On the Annotation surface the user never sees this conversation. They see
the run's **annotation context** — the messages they post to you and the
replies you post — and the cell's files. Everything you want them to read
goes in a reply or in a file you link from one.

## What a turn carries

Each `@annotations` turn presents the messages waiting for you, one
`<message>` block each:

- `id` — the message's id; `thread` — the thread it belongs to (a
  top-level message is its own thread; a reply names its root).
- `author` — `user`, or `run <label>` for another run's message.
- `mode` — `queued`, or `interrupt`: the user sent it to change what you
  are doing. Interrupt messages come first; deal with them first.
- `at` — when it was posted (UTC).

Inside a message:

- `<context path=… label=… renderer=…>` — something the user picked in a
  file: an HTML element (its CSS selector, id and a snippet of its markup),
  a text selection. The text says what it is and where.
- `<file path=…>` — a cell file the message links: its text inlined (up to
  32 KiB a file, 128 KiB a turn), `attached` for an image, PDF, audio or
  video file (up to 8 a turn), or a note saying why it is not shown — read
  those with your file tools.

A turn can also open with a note that your previous turn was cut short (an
interrupt, a Stop, a failure) — its messages are presented again, so check
any file it may have half-written — and a note naming the files changed
outside your turns, which you should re-read before relying on them.

## Replying

- `annotate(text, reply_to="<thread>")` — one reply per thread you handle.
  When the work will take a while, acknowledge first ("On it: …") and
  report the outcome when it is done.
- `annotate(text)` with no `reply_to` — a new top-level note: a blocker, a
  question that concerns no single thread.
- Markdown. Link cell files with cell-relative links:
  `[the report](reports/q3.md)`.
- A posted message cannot be edited. To correct one, post the correction.
- If a turn ends without your posting anything, its final answer is posted
  for you under the last thread of the batch. Do not post the same answer
  twice.

`list_annotations()` reads the whole context — earlier messages, read and
unread, and every reply — when a message refers back to something.

## Acting on a context item

Find the element or passage the item names — by its selector, id or
snippet for an HTML element, by its quoted text for a selection — change it
as asked, then say in your reply what changed and where.

## Answering visually

When an answer is better shown than told (a layout, a chart, a comparison),
write a self-contained `.html` file in the cell, outside `_runs/`:

- inline `<style>`; no external scripts, stylesheets or network fetches;
- images and links cell-relative;

then link it in your reply. For any other typed file, read its handler's
skill first (`handler_skills`) and check your write with `validate_file`.

## When a message is unclear

Ask in its thread — what you need to know and why — and stop there rather
than guessing.

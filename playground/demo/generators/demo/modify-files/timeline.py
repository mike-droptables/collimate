"""Modify files — a scripted file-editing agent.

Reads the input cell's files, logs a plan, then edits them one at a time so
each commit shows up as a live version: markdown files get a summary line
under their title, list files become checklists, a budget gets its total.
``CHANGES.md`` records every edit as a unified diff and is opened last.

The prompt is shown in the log but not interpreted — the edits are scripted
so the demo is the same for everyone. No model is called.
"""
from __future__ import annotations

import difflib
import re

SPINNING_UP = "spinning_up"
RUNNING = "running"
FINISHING = "finishing"

PACE = {"relaxed": 1.6, "normal": 1.0, "brisk": 0.55}

# What the demo edits when it is run without an input cell.
BUILT_IN_INPUT = {
    "name": "Example note",
    "files": {
        "reading-list.md": (
            "# Reading list\n\n"
            "- The Pragmatic Programmer\n"
            "- A Philosophy of Software Design\n"
            "- Working in Public\n"
        ),
    },
}

_AMOUNT = re.compile(r"^(?P<label>.*?\S)\s{2,}(?P<amount>-?\d+(?:\.\d+)?)\s*$")


def is_list_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return "list" in name or "todo" in name


def describe_markdown(text: str) -> str:
    lines = text.splitlines()
    sections = sum(1 for line in lines if line.startswith("## "))
    items = sum(1 for line in lines if re.match(r"^\s*[-*]\s", line))
    words = len(text.split())
    parts = [f"{words} words"]
    if sections:
        parts.append(f"{sections} section{'s' if sections != 1 else ''}")
    if items:
        parts.append(f"{items} list item{'s' if items != 1 else ''}")
    return ", ".join(parts)


def edit_markdown(text: str, *, add_summary: bool, checklist: bool) -> tuple[str, list[str]]:
    notes: list[str] = []
    lines = text.splitlines()
    if checklist:
        converted = 0
        for i, line in enumerate(lines):
            match = re.match(r"^(\s*[-*])\s+(?!\[[ xX]\])(.+)$", line)
            if match:
                lines[i] = f"{match.group(1)} [ ] {match.group(2)}"
                converted += 1
        if converted:
            notes.append(f"turned {converted} bullet{'s' if converted != 1 else ''} into checkboxes")
    if add_summary and not any(line.startswith("> **Summary:**") for line in lines):
        summary = f"> **Summary:** {describe_markdown(text)} — tidied by the demo agent."
        title_at = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
        if title_at is None:
            lines[0:0] = [summary, ""]
        else:
            lines[title_at + 1:title_at + 1] = ["", summary]
        notes.append("added a summary block under the title")
    return "\n".join(lines).rstrip("\n") + "\n", notes


def edit_budget(text: str) -> tuple[str, list[str]]:
    lines = [line.rstrip() for line in text.splitlines()]
    total = 0.0
    counted = 0
    width = 0
    for line in lines:
        match = _AMOUNT.match(line)
        if match and not line.lower().startswith("total"):
            total += float(match.group("amount"))
            counted += 1
            width = max(width, len(line))
    if not counted:
        return text, ["no amounts found — left as is"]
    lines = [line for line in lines if not line.lower().startswith("total")]
    while lines and not lines[-1]:
        lines.pop()
    amount = f"{total:.2f}".rstrip("0").rstrip(".")
    lines += ["", ("Total" + " " * max(2, width - len("Total") - len(amount)) + amount)]
    return "\n".join(lines) + "\n", [f"summed {counted} lines into a total of {amount}"]


def edit_plain(text: str) -> tuple[str, list[str]]:
    return "\n".join(line.rstrip() for line in text.splitlines()).rstrip("\n") + "\n", ["trimmed trailing whitespace"]


def edit_file(path: str, text: str, params: dict) -> tuple[str, list[str]]:
    """The edited text and the notes describing it; an unchanged file says so
    with one uniform note whatever edit was attempted."""
    lower = path.lower()
    if lower.endswith((".md", ".markdown")):
        after, notes = edit_markdown(text, add_summary=bool(params.get("add_summary", True)),
                                     checklist=bool(params.get("checklist_items", True)) and is_list_file(path))
    elif "budget" in lower or "cost" in lower or "expenses" in lower:
        after, notes = edit_budget(text)
    else:
        after, notes = edit_plain(text)
    if after == text:
        return text, ["already tidy — no change"]
    return after, notes


def unified_diff(path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=2,
    ))


def changes_markdown(prompt: str, source: str, edits: list[tuple[str, list[str], str]]) -> str:
    out = ["# Changes", "", f"Scripted edits to **{source}** by the demo agent.", ""]
    if prompt.strip():
        out += ["> Prompt: " + prompt.strip().replace("\n", " "), ""]
    out += ["| File | Change |", "| --- | --- |"]
    for path, notes, _ in edits:
        out.append(f"| `{path}` | {'; '.join(notes)} |")
    out.append("")
    for path, _, diff in edits:
        if not diff:
            continue
        out += [f"## {path}", "", "```diff", diff.rstrip("\n"), "```", ""]
    out += ["---", "", "This run is a demo: the plan and the edits are scripted, and no model was called.", ""]
    return "\n".join(out)


def text_files(files: dict) -> dict[str, str]:
    """The input's text files, in a stable order (paths the demo can edit)."""
    out: dict[str, str] = {}
    for path in sorted(files):
        content = files[path]
        if isinstance(content, bytes):
            try:
                content = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
        # Run folders hold other runs' UI files: read-only, never edited.
        if path.startswith("_runs/") or path.split("/")[-1].startswith("."):
            continue
        out[path] = str(content)
    return out


async def play(demo) -> None:
    factor = PACE.get(str(demo.params.get("pace", "normal")), 1.0)

    async def pause(seconds: float) -> None:
        await demo.sleep(seconds * factor)

    demo.phase(SPINNING_UP, "Reading the input cell")
    demo.log("Demo agent: the plan and the edits below are scripted — no model is called.")
    if demo.prompt.strip():
        demo.log(f"Prompt (shown, not interpreted): {demo.prompt.strip()}")
    source = demo.inputs[0] if demo.inputs else BUILT_IN_INPUT
    files = text_files(source.get("files", {}))
    if not files:
        demo.log("The input cell has no text files — editing the built-in example instead.")
        source = BUILT_IN_INPUT
        files = text_files(source["files"])
    await pause(1.4)
    demo.log(f"Read {len(files)} file{'s' if len(files) != 1 else ''} from {source.get('name', 'the input')}:")
    for path, text in files.items():
        demo.log(f"  {path} ({len(text.splitlines())} lines)")
        await pause(0.5)

    await pause(1.2)
    demo.log("Plan:")
    demo.log("  1. Add a one-line summary under each markdown title.")
    demo.log("  2. Turn list files into checklists.")
    demo.log("  3. Total any budget file.")
    demo.log("  4. Record every edit in CHANGES.md and open it.")
    await pause(2.0)

    demo.phase(RUNNING, "Editing files")
    # Explicitly retain the input name in this scripted commit; an unnamed
    # commit leaves the cell name unchanged.
    cell_name = str(source.get("name", "the input"))
    edits: list[tuple[str, list[str], str]] = []
    for path, before in files.items():
        demo.log(f"Editing {path} …")
        await pause(2.2)
        after, notes = edit_file(path, before, demo.params)
        diff = unified_diff(path, before, after) if after != before else ""
        edits.append((path, notes, diff))
        # Every file is written as a copy, changed or not, so the target cell
        # holds the whole set whether the run edits in place or fills a new cell.
        demo.write(path, after)
        await demo.commit(cell_name=cell_name)
        demo.log(f"  {path}: {'; '.join(notes)}")
        await pause(1.2)

    demo.phase(FINISHING, "Writing CHANGES.md")
    demo.log("Checking the result …")
    await pause(1.6)
    demo.write("CHANGES.md", changes_markdown(demo.prompt, str(source.get("name", "the input")), edits))
    await demo.reveal_file("CHANGES.md")
    changed = sum(1 for _, _, diff in edits if diff)
    demo.log(f"Done: {changed} of {len(edits)} files changed, all copied. CHANGES.md is open.")
    demo.finish(success=True, message=f"Edited {changed} file{'s' if changed != 1 else ''} (scripted)")

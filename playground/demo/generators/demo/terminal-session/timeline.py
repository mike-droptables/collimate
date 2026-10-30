"""Terminal session — a scripted shell in the live terminal renderer.

Plays a git clone, a test run with a progress bar and a summary, all in ANSI
colour, then leaves the prompt open for a while. Whatever the visitor types is
echoed back; pressing Enter answers with one line saying the demo shell does
not execute commands. Nothing here touches a network or a real shell.

The script runs against the playground's timeline contract (``demo``): the
shim serves ``demo.terminal(label)`` over its own demo websocket route and
the terminal renderer replays the bytes written to it.
"""
from __future__ import annotations

import asyncio

SPINNING_UP = "spinning_up"
RUNNING = "running"
AWAITING_INPUT = "awaiting_input"
FINISHING = "finishing"

PACE = {"relaxed": 1.6, "normal": 1.0, "brisk": 0.55}

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
CYAN = "\x1b[36m"
NL = "\r\n"

PROMPT = f"{BOLD}{GREEN}demo@playground{RESET}:{BOLD}{BLUE}~/widgets{RESET}$ "
HOME_PROMPT = f"{BOLD}{GREEN}demo@playground{RESET}:{BOLD}{BLUE}~{RESET}$ "
NOT_EXECUTED = f"{DIM}demo shell — commands are not executed here{RESET}"

# The test files "pytest" walks, with how many tests each holds. The one
# skip lands in test_export so the summary line has something to colour.
TEST_FILES = (
    ("tests/test_models.py", 9, ""),
    ("tests/test_parsers.py", 12, ""),
    ("tests/test_export.py", 7, "s"),
    ("tests/test_cli.py", 6, ""),
    ("tests/test_render.py", 6, ""),
)
TOTAL_TESTS = sum(n for _, n, _ in TEST_FILES)


class Shell:
    """Line discipline for typed input: echo, backspace, and one answer per
    Enter. The callback the shim invokes is synchronous; each keystroke is
    handled on the running loop and the scheduled task is returned so a shim
    that awaits the callback's result can do so."""

    def __init__(self, term, prompt: str):
        self.term = term
        self.prompt = prompt
        self.line = ""
        self.open = False

    def on_input(self, data):
        return asyncio.ensure_future(self.handle(data))

    async def handle(self, data) -> None:
        if not self.open:
            return
        text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
        if text.startswith("\x1b"):
            return  # arrow keys and other escape sequences: nothing to echo
        for ch in text:
            if ch in ("\r", "\n"):
                await self.term.write(NL)
                if self.line.strip():
                    await self.term.write(NOT_EXECUTED + NL)
                self.line = ""
                await self.term.write(self.prompt)
            elif ch == "\x7f":
                if self.line:
                    self.line = self.line[:-1]
                    await self.term.write("\b \b")
            elif ch == "\x03":
                self.line = ""
                await self.term.write("^C" + NL + self.prompt)
            elif ch.isprintable():
                self.line += ch
                await self.term.write(ch)


async def play(demo) -> None:
    factor = PACE.get(str(demo.params.get("pace", "normal")), 1.0)
    keep_open = float(demo.params.get("keep_open_seconds", 15) or 0)

    async def pause(seconds: float) -> None:
        await demo.sleep(seconds * factor)

    demo.phase(SPINNING_UP, "Opening the demo shell")
    demo.log("Demo terminal: everything below is scripted — no commands run.")
    term = demo.terminal("Demo shell")
    shell = Shell(term, PROMPT)
    term.on_input(shell.on_input)
    # A run on a cell that holds files keeps that cell's name (the files
    # stay too); an empty cell is named after the shell.
    await demo.commit(cell_name=cell_name_for(demo, "Demo terminal"))
    await demo.reveal_file(term.pointer_path)
    await pause(0.8)

    async def out(text: str) -> None:
        await term.write(text)

    async def typed(command: str) -> None:
        for ch in command:
            await out(ch)
            await pause(0.09 if ch != " " else 0.16)
        await pause(0.35)
        await out(NL)

    demo.phase(RUNNING, "Cloning the repository")
    await out(f"{DIM}Welcome to the Collimate demo shell. Output is scripted.{RESET}{NL}{NL}")
    await out(HOME_PROMPT)
    await pause(0.6)
    await typed("git clone https://example.com/acme/widgets.git")
    await out("Cloning into 'widgets'..." + NL)
    await pause(0.5)
    await out("remote: Enumerating objects: 412, done." + NL)
    await pause(0.3)
    await out("remote: Counting objects: 100% (412/412), done." + NL)
    await pause(0.3)
    await out("remote: Compressing objects: 100% (198/198), done." + NL)
    await pause(0.3)
    for pct, count, mib in ((9, 37, 0.3), (23, 95, 0.8), (41, 169, 1.4), (58, 239, 2.0), (76, 313, 2.6), (92, 379, 3.1)):
        await out(f"\rReceiving objects:  {pct:2d}% ({count}/412), {mib:.1f} MiB | 2.3 MiB/s")
        await pause(0.28)
    await out("\rReceiving objects: 100% (412/412), 3.4 MiB | 2.3 MiB/s, done." + NL)
    await pause(0.3)
    await out("Resolving deltas: 100% (201/201), done." + NL)
    await pause(0.5)

    demo.phase(RUNNING, "Running the test suite")
    demo.log("Cloned widgets (scripted). Running the tests.")
    await out(HOME_PROMPT)
    await pause(0.5)
    await typed("cd widgets && pytest -q")
    await out(f"{BOLD}============================= test session starts =============================={RESET}{NL}")
    await out("platform linux -- Python 3.13.2, pytest-8.3.4" + NL)
    await out(f"collected {TOTAL_TESTS} items{NL}{NL}")
    await pause(0.6)
    done = 0
    for path, count, marks in TEST_FILES:
        # One row per file, rewritten in place with \r + erase-line. The row
        # is kept under 50 columns so it never wraps in a narrow cell — a
        # wrapped row cannot be overwritten and would print once per tick.
        name = path.rsplit("/", 1)[-1].ljust(16)
        dots = ""
        for i in range(count):
            done += 1
            dots += f"{YELLOW}s{RESET}" if marks and i == count - 1 else f"{GREEN}.{RESET}"
            bar = progress_bar(done, TOTAL_TESTS)
            await out(f"\r\x1b[2K{name} {dots.ljust(12 + len(dots) - i - 1)}  {bar}")
            await pause(0.12)
        await out(NL)
        await pause(0.2)
    await out(NL)
    await out(f"{BOLD}{GREEN}{TOTAL_TESTS - 1} passed{RESET}, {YELLOW}1 skipped{RESET} in 6.91s{NL}")
    await pause(0.8)

    demo.phase(RUNNING, "Summarising")
    await out(PROMPT)
    await pause(0.4)
    await typed("git log --oneline -3")
    await out(f"{YELLOW}a41c9e2{RESET} Export: write the summary table last{NL}")
    await out(f"{YELLOW}7f03b1d{RESET} Parsers: accept trailing commas{NL}")
    await out(f"{YELLOW}c2e88a0{RESET} Initial import{NL}")
    await pause(0.7)
    box_lines = (
        f"{CYAN}┌──────────────────────────────────────────┐{RESET}",
        f"{CYAN}│{RESET} {BOLD}Summary{RESET}                                  {CYAN}│{RESET}",
        f"{CYAN}│{RESET} repository  widgets (412 objects)        {CYAN}│{RESET}",
        f"{CYAN}│{RESET} tests       {GREEN}{TOTAL_TESTS - 1} passed{RESET}, {YELLOW}1 skipped{RESET}         {CYAN}│{RESET}",
        f"{CYAN}│{RESET} status      {GREEN}ready{RESET}                        {CYAN}│{RESET}",
        f"{CYAN}└──────────────────────────────────────────┘{RESET}",
    )
    for line in box_lines:
        await out(line + NL)
        await pause(0.12)
    demo.log("Test run summarised (scripted): 39 passed, 1 skipped.")

    await out(NL + f"{DIM}Type here — the demo shell echoes what you type and executes nothing.{RESET}{NL}")
    await out(PROMPT)
    shell.open = True
    if keep_open > 0:
        demo.phase(AWAITING_INPUT, "The shell is open — type into it")
        demo.log(f"Shell open for {keep_open:.0f}s of typing, then the run completes.")
        await demo.sleep(keep_open)
    shell.open = False

    demo.phase(FINISHING, "Closing the shell")
    await out(NL + f"{DIM}[demo shell closed — run Terminal session again to replay]{RESET}{NL}")
    await pause(0.3)
    demo.finish(success=True, message="Scripted shell session finished")


def cell_name_for(demo, fresh: str) -> str:
    """The target cell's own name when it already holds files (a run on an
    existing cell must not rename it), else ``fresh`` — an empty cell is
    named after what the run puts in it."""
    if demo.inputs and demo.inputs[0].get("files") and demo.inputs[0].get("name"):
        return str(demo.inputs[0]["name"])
    return fresh


def progress_bar(done: int, total: int, width: int = 10) -> str:
    filled = round(width * done / total)
    pct = round(100 * done / total)
    return f"{DIM}[{RESET}{GREEN}{'█' * filled}{RESET}{DIM}{'░' * (width - filled)}]{RESET} {pct:3d}%"

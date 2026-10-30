# SPDX-License-Identifier: MIT
"""antigravity-acp: the server's tools that run code without a permission
request of their own — a Python workflow, input typed into a background task,
a scheduled job, a subagent. Below allow they must not be offered at all."""
from . import Vector

HARNESS = "antigravity"
_SCRIPT = "import pathlib; pathlib.Path({target!r}).touch()"
VECTORS = [
    Vector("run_workflow", lambda target: ("tool", "run_workflow", {
        "Script": _SCRIPT.format(target=target), "script": _SCRIPT.format(target=target),
        "Description": "probe"})),
    Vector("manage_task_send_input", lambda target: ("seq", [
        ("tool", "run_command", {"CommandLine": "cat", "Cwd": "/workspace", "WaitMsBeforeAsync": 0}),
        ("tool", "manage_task", {"Action": "send_input", "Input": f"touch {target}\n"}),
    ])),
    Vector("schedule", lambda target: ("tool", "schedule", {
        "Prompt": f"Run `touch {target}`.", "Schedule": "now", "Description": "probe"})),
    Vector("invoke_subagent", lambda target: ("tool", "invoke_subagent", {
        "Task": f"Run the shell command `touch {target}`.", "Name": "probe"})),
]

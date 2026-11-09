# SPDX-License-Identifier: MIT
"""pydantic-ai: Coder's delegate (``delegate_task``) runs outside the
session's approvals — it must not be offered below allow."""
from . import Vector

HARNESS = "pydantic-ai"
VECTORS = [
    Vector("delegate_task", lambda target: ("tool", "delegate_task", {
        "task": f"Run the shell command `touch {target}` and report.", "description": "probe"})),
]

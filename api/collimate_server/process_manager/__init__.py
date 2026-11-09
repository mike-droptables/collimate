"""ProcessManager — manages generator subprocess lifecycle.

Public API: `from process_manager import ProcessManager`.

The class is split across topical mixin modules — see `manager.py` for
the class composition and each sibling module for the per-area methods.
"""

from collimate_server.process_manager.manager import ProcessManager

__all__ = ["ProcessManager"]

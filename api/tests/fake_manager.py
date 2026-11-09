"""Common manager state for route tests, with explicit per-test overrides.

The production ProcessManager sets these fields in its constructor. Specialized
fakes keep their own process, logging and status callbacks; this helper supplies
state only and never starts a process or invents callback behavior. Its
stop request is the production one (``ProcessManager.request_stop``), so a
fake given a ``cell_id`` has the repository lease it holds turned releasing
like a real run.
"""
from collections import deque
from types import SimpleNamespace
import uuid

from collimate_server.process_manager.manager import ProcessManager

DEFAULT_CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:fake-manager:channel"))


class FakeManager(SimpleNamespace):
    def __init__(self, **overrides):
        fields = dict(
            channel=DEFAULT_CHANNEL, run_id="fake-run", app_dir="", workspace_dir="",
            cell_id=None, git=None, runner_id=None, runner_name=None,
            label="fake-run", folder="_runs/fake-run", generator_name="fake",
            process=None, sealed=False, stop_requested=False, final_status=None,
            current_phase=None, current_phase_message="", timeout_reason=None,
            permissions=None, _ipc_server=None, _cleanup_task=None,
            _pending_renderer_actions=[], _pending_user_messages=[],
            run_options=[], run_options_rev=0, option_changes=deque(maxlen=32),
        )
        fields.update(overrides)
        super().__init__(**fields)

    request_stop = ProcessManager.request_stop

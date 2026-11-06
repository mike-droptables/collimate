# SPDX-License-Identifier: MIT
"""The Claude Code TTY's privacy witnesses, shared by the live Claude TTY
tests: the pinned native versions, the transcript size bound, and the
checks that the selected key stays in the cell's private harness home and
out of every native process's argv and environment on this machine. Re-exports what those tests
bind with (the backend module, the account helpers, the evidence writer)."""
import stat

from _agent_shared.harnesses.claude_code import backend as claude
from tests_live._host_terminal import _key_in_home, _key_values
from tests_live.conftest import bind_model_account, model_account, run_on_test_loop
from tests_live.test_live_claude_antigravity_generator_capture import write_evidence

__all__ = ["PINS", "MAX_TRANSCRIPT", "private_home", "private_processes", "claude",
           "bind_model_account", "model_account", "run_on_test_loop", "write_evidence"]

PINS = {'claude-code': '2.1.310', 'claude-agent-acp': '0.95.0'}
MAX_TRANSCRIPT = 4 * 1024 * 1024


def private_home(home):
    """The harness home is private (0700, its key files 0600 and real
    files), and the selected key sits only in the key file, never in a
    session record the run carries out."""
    assert home.is_dir() and not home.is_symlink()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    for name in (claude._API_KEY_FILE, claude._API_KEY_HELPER):
        path = home / name
        assert path.is_file() and not path.is_symlink()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    carried, kept = _key_in_home('claude-code')
    assert carried == []
    assert kept and all(row[0] == claude._API_KEY_FILE for row in kept)


def private_processes(term, config_home):
    """No held key in the argv or environment of any process of the TTY's
    session on this machine (its process group, read from ``/proc``), the
    leader running out of the entry's per-account home
    (``CLAUDE_CONFIG_DIR``). No fixture-only marker is injected into its
    launch."""
    argv, environments = [], []
    for pid in term.group():
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as fh:
                argv.append(fh.read().replace(b'\0', b' ').decode('utf-8', 'replace'))
            with open(f'/proc/{pid}/environ', 'rb') as fh:
                environments.append(fh.read().replace(b'\0', b'\n').decode('utf-8', 'replace'))
        except OSError:
            continue
    assert argv and environments, 'Could not inspect the native processes'
    with open(f'/proc/{term.proc.pid}/environ', 'rb') as fh:
        leader = fh.read().decode('utf-8', 'replace').split('\0')
    assert 'CLAUDE_CONFIG_DIR=' + config_home in leader
    if any(value in body for value in _key_values().values()
           for body in (*argv, *environments)):
        raise AssertionError('Held key reached native argv or process environment')

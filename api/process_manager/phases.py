"""Run lifecycle phase vocabulary — the sub-state *within* a 'running' run.

Distinct from ``runs.status`` (the terminal-truth column: running / completed
/ failed / stopped / timed_out / interrupted). ``runs.phase`` is what drives the cell's "loading /
spinning up" vs "actively running" indicator, and is mirrored to the terminal
status at run end so a reconnecting client can render the right state.

This module is the single source of truth for the string values. The SDK
re-declares the same literals (``collimate/native.py``) because it can't
import api modules — keep the two in sync.
"""

# Non-terminal progression, in order. The auto-machine only *advances*:
# a backend-auto transition to RUNNING is suppressed once the run is already
# past spinning_up (see _apply_phase's caller guards).
QUEUED = "queued"            # row inserted, generator not yet dispatched
SPINNING_UP = "spinning_up"  # subprocess spawned, booting (imports,
                             # container pull, clone, model warm-up)
RUNNING = "running"          # real work underway
AWAITING_INPUT = "awaiting_input"  # session open, generator idle until the
                                   # user sends a message (wallclock watchdog
                                   # suspends while here)
AWAITING_APPROVAL = "awaiting_approval"  # an approval card (gen.ask) waits on
                                         # the user mid-turn; the SDK holds the
                                         # run here until the last card closes
                                         # (watchdog paused, turn budget kept)
FINISHING = "finishing"      # committing results (optional, generator-set)

# Terminal — mirror of runs.status.
COMPLETED = "completed"
FAILED = "failed"
STOPPED = "stopped"
TIMED_OUT = "timed_out"
INTERRUPTED = "interrupted"

ACTIVE_PHASES = (QUEUED, SPINNING_UP, RUNNING, AWAITING_INPUT, AWAITING_APPROVAL,
                 FINISHING)
TERMINAL_PHASES = (COMPLETED, FAILED, STOPPED, TIMED_OUT, INTERRUPTED)
ALL_PHASES = ACTIVE_PHASES + TERMINAL_PHASES

# The phases a generator may set over IPC (gen.phase), matching the SDK's
# ``native.phase`` docstring. QUEUED and the terminal phases are host-owned:
# failure is not a phase, and a run only ends through its outcome.
GENERATOR_PHASES = (SPINNING_UP, RUNNING, AWAITING_INPUT, AWAITING_APPROVAL,
                    FINISHING)

# Phases that an auto "running" fallback is allowed to overwrite (i.e. the run
# hasn't visibly started real work yet). Once a generator or the machine has
# set RUNNING/FINISHING/terminal, the auto fallback no-ops.
PRE_RUNNING_PHASES = (None, QUEUED, SPINNING_UP)


def is_terminal(phase: str | None) -> bool:
    return phase in TERMINAL_PHASES


def valid_phase(phase: str | None) -> bool:
    return phase in ALL_PHASES

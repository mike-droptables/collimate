"""Where cells live on the canvas.

A fork from the files pane's History rail, a duplicate and a "Run in new
cell" clone land in a brand-new stack right of their source's stack
(``insert_new_stack_right_of``).

Every channel has exactly one Capabilities stack (``capabilities_stack``),
open on the board or hidden to the top bar; ``genesis.ensure_capabilities_stack``
is the only code that creates it. It leads with the app's built-in cards
(``Cell.builtin``): inserts land after them (``builtin_head``). The cells after
the cards are the channel's capability catalog, read through one walker
(``capabilities_stack_cells``).
"""

from collections.abc import Iterator
from typing import Final

from models import Cell, Channel, Stack

CAPABILITIES_STACK_TYPE: Final = "capabilities"
CAPABILITIES_STACK_NAME: Final = "Capabilities"


def iter_stacks(channel: Channel) -> Iterator[Stack]:
    """Iterate the owning channel's main stacks, then navigation stacks."""
    yield from channel.main_stacks
    yield from channel.nav_stacks


def find_cell(channel: Channel | None, cell_id) -> tuple[Stack | None, Cell | None]:
    """Find a canvas cell in an explicit channel; missing owners never fall back."""
    if channel is not None:
        cid = str(cell_id)
        for stack in iter_stacks(channel):
            for cell in stack.cells:
                if str(cell.id) == cid:
                    return stack, cell
    return None, None


def is_capabilities_stack(stack: Stack) -> bool:
    """Whether ``stack`` is the channel's Capabilities stack."""
    return stack.stack_type == CAPABILITIES_STACK_TYPE


def capabilities_stack(channel: Channel) -> Stack:
    """The channel's Capabilities stack, open (main) or hidden (navigation).
    Raises ``LookupError`` when it is missing: after
    ``genesis.ensure_capabilities_stack`` that is a bug."""
    for stack in iter_stacks(channel):
        if is_capabilities_stack(stack):
            return stack
    raise LookupError("Channel has no Capabilities stack")


def capabilities_stack_cells(channel: Channel) -> list[Cell]:
    """The channel's capability cells: the Capabilities stack's cells after
    the built-in cards, in stack order."""
    return [cell for cell in capabilities_stack(channel).cells if not cell.builtin]


# The one answer to every layout operation a built-in card refuses.
BUILTIN_REFUSAL = "Built-in packages are part of the app"


def builtin_head(stack: Stack) -> int:
    """How many built-in cards lead ``stack``. The cards always sit at the
    head of the Capabilities stack, so every insert lands at or after this."""
    count = 0
    for cell in stack.cells:
        if not cell.builtin:
            break
        count += 1
    return count


def _stack_index(channel: Channel, stack_id) -> int | None:
    sid = str(stack_id) if stack_id is not None else None
    if sid is None:
        return None
    for i, s in enumerate(channel.main_stacks):
        if str(s.id) == sid:
            return i
    return None


def insert_new_stack_right_of(channel: Channel, ref_stack_id) -> Stack:
    """Insert a brand new empty stack immediately right of the referenced stack
    in main_stacks. Existing stacks at and beyond that index shift right.
    Returns the new Stack."""
    idx = _stack_index(channel, ref_stack_id)
    if idx is None:
        raise ValueError(f"reference stack {ref_stack_id} not found in main_stacks")
    new_stack = Stack(name="", cells=[])
    channel.main_stacks.insert(idx + 1, new_stack)
    return new_stack

"""Stack placement and lifecycle endpoints.

Removing one cell may carry a remove zone's tag action, applied to the
removed record only. Duplicates copy the source's name, icon and tags.

The channel's one Capabilities stack is hidden to the top bar, never removed,
renamed, duplicated or emptied away, and its cells are not removed: a cell
moves off it first. A cell moves between any two stacks; it enters
Capabilities only while nothing runs on it, and never before the built-in
cards at its head, which refuse every removal, move and duplicate
(``BUILTIN_REFUSAL``).

Async handlers offload blocking storage/publication to worker threads. The
lifespan writer coalesces ordinary layout saves; consuming operations cross a
durability barrier before acknowledgment. Cell locks precede short state locks,
and SQLite work runs outside the state lock. Files and cell metadata use their
own explicit publishers.
"""

from cell_placement import BUILTIN_REFUSAL, builtin_head, iter_stacks, is_capabilities_stack
import asyncio
import logging
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from models import Cell, Stack
from state import require_channel, persist_state, state_lock
from routes.layout import channel_layout, stripped_channel
from cell_lineage import announce_lineage

router = APIRouter()
logger = logging.getLogger(__name__)


def _stack_cell_ids(channel: str, stack_id: UUID) -> set[str]:
    ch = require_channel(channel)
    stack = next((s for s in iter_stacks(ch) if s.id == stack_id), None)
    return {str(c.id) for c in stack.cells} if stack is not None else set()


def _with_stack_cells_locked(channel: str, stack_id: UUID, body, *, hold_state=True):
    """Serialize the stack's cells, retrying a changing membership snapshot.

    Consumption uses short state sections inside its body and leaves SQL outside
    state_lock. Pure layout moves retain the state lock for their publication.
    """
    from cell_write_scope import cell_write_scope
    from state import channel_db_name

    for _ in range(5):
        with state_lock:
            ids = _stack_cell_ids(channel, stack_id)
            identity = channel_db_name(require_channel(channel))
        with cell_write_scope(identity, ids, hold_state=hold_state):
            with state_lock:
                ready = _stack_cell_ids(channel, stack_id) <= ids
            if ready:
                return body()
    raise HTTPException(409, "The stack changed while it was being removed — try again")


def _pop_main_stack(ch, stack) -> None:
    """Pop ``stack`` (found by identity, not a stale index) from main_stacks
    and repair the selection. The caller holds ``state_lock``."""
    idx = next(i for i, s in enumerate(ch.main_stacks) if s is stack)
    ch.main_stacks.pop(idx)
    if ch.main_selection_id == stack.id:
        # Selection follows the stack to the left; if the removed stack
        # was leftmost, the old right neighbor now sits at index 0.
        ch.main_selection_id = ch.main_stacks[max(0, idx - 1)].id if ch.main_stacks else None


@router.post("/api/stacks/{channel}/create")
async def create_stack(channel: str):
    def _body():
        ch = require_channel(channel)
        with state_lock:
            stack = Stack(name="", cells=[Cell(name="New Cell")])
            ch.main_stacks.append(stack)
            ch.main_selection_id = stack.id
        persist_state()
        return stripped_channel(ch)
    result = await asyncio.to_thread(_body)
    from state import flush_persist_state
    await asyncio.to_thread(flush_persist_state)
    return result


class InsertBlankCellRequest(BaseModel):
    # Client-minted id so the client can address the cell before the
    # response lands. Generated if omitted.
    id: str | None = None
    name: str = "New cell"


@router.post("/api/stacks/{channel}/{stack_id}/insert_blank_cell")
async def insert_blank_cell(channel: str, stack_id: UUID, body: InsertBlankCellRequest):
    """Create a blank Standard cell in its own new stack, placed right
    of the given anchor stack. Used by New cell (the top bar ＋, Alt+N)
    through useSceneActions.newEmptyStackRight. No generator, no draft
    state: an empty cell the user can author into."""
    def _body():
        import uuid as _uuid
        ch = require_channel(channel)
        try:
            cell_id = _uuid.UUID(body.id) if body.id else _uuid.uuid4()
        except ValueError:
            raise HTTPException(400, "id must be a valid UUID")
        with state_lock:
            try:
                src_idx = next(i for i, s in enumerate(ch.main_stacks) if s.id == stack_id)
            except StopIteration:
                raise HTTPException(404, "Stack not found")
            new_cell = Cell(id=cell_id, name=body.name, files=[])
            new_stack = Stack(name="", cells=[new_cell])
            ch.main_stacks.insert(src_idx + 1, new_stack)
            ch.main_selection_id = new_stack.id
        persist_state()
        return stripped_channel(ch)
    result = await asyncio.to_thread(_body)
    from state import flush_persist_state
    await asyncio.to_thread(flush_persist_state)
    return result


# --- STACK OPERATIONS ---

@router.post("/api/stacks/{channel}/{stack_id}/remove")
async def remove_stack(channel: str, stack_id: UUID):
    """Remove a stack from the canvas. Each cell keeps its history and a
    ``removed_cells`` record, restorable from the Removed shelf. Pending
    edits are checkpointed first so no user work is lost. Capabilities is
    refused (400): it is hidden, not removed.
    """
    def _body():
        from state import channel_db_name
        from cell_catalog import remember_removed_cells
        from run_registry import running_cell_ids

        with state_lock:
            ch = require_channel(channel)
            stack = next((s for s in ch.main_stacks if s.id == stack_id), None)
            if stack is None:
                return channel_layout(ch), []
            if is_capabilities_stack(stack):
                raise HTTPException(400, "Capabilities is hidden, not removed")
            cells = list(stack.cells)
            if any(str(c.id) in running_cell_ids() for c in cells):
                raise HTTPException(409, "Stack contains a running cell — stop the run first")

        # Materialize even never-versioned/blank cells before unmounting.
        # Failure leaves the canvas intact instead of silently losing work.
        db_name = channel_db_name(ch)
        removed = [str(c.id) for c in cells]
        from cell_metadata import advance_cell_metadata_generations
        advance_cell_metadata_generations(db_name, removed)
        remember_removed_cells(db_name, stack, cells)

        with state_lock:
            if require_channel(channel) is not ch or not any(s is stack for s in ch.main_stacks):
                raise HTTPException(409, "The stack changed while it was being removed")
            _pop_main_stack(ch, stack)
            return channel_layout(ch), removed
    result, removed = await asyncio.to_thread(_with_stack_cells_locked, channel, stack_id, _body, hold_state=False)
    from state import flush_persist_state
    await asyncio.to_thread(persist_state)
    await asyncio.to_thread(flush_persist_state)
    announce_lineage(channel, removed)
    return result


class RemoveCellBody(BaseModel):
    """A remove zone's action: tags added to, then taken from, the removed
    record (``cell_tags.apply_tag_action``). The lists are typed loosely so a
    non-string item is the route's 400, not a 422."""
    add_tags: list = []
    remove_tags: list = []


@router.post("/api/channels/{channel}/cells/{cell_id}/remove")
async def remove_cell(channel: str, cell_id: UUID, body: Optional[RemoveCellBody] = None):
    """Take the canvas cell ``cell_id`` off its stack, recording it on the
    Removed shelf first; a stack leaves the canvas when it empties. The
    optional body's tag action applies under the cell lock to the removed
    record's tags only; the live cell is never changed. Refuses a built-in
    card (400), a Capabilities cell (400: it moves off Capabilities first), a
    running cell (409), an invalid tag action (400) and a cell that is not on
    the main canvas (404). Returns the channel layout."""
    from state import channel_db_name
    from cell_catalog import remember_removed_cells
    from cell_tags import apply_tag_action
    from run_registry import running_cell_ids

    action = body if body is not None and (body.add_tags or body.remove_tags) else None

    def _stack_id() -> UUID:
        with state_lock:
            ch = require_channel(channel)
            stack = next((s for s in ch.main_stacks if any(c.id == cell_id for c in s.cells)), None)
            if stack is None:
                raise HTTPException(404, "Cell is not on the canvas")
            return stack.id

    def _body():
        with state_lock:
            ch = require_channel(channel)
            stack = next((s for s in ch.main_stacks if s.id == stack_id), None)
            cell = next((c for c in stack.cells if c.id == cell_id), None) if stack is not None else None
            if stack is None or cell is None:
                raise HTTPException(409, "The cell moved while it was being removed — try again")
            if cell.builtin:
                raise HTTPException(400, BUILTIN_REFUSAL)
            if is_capabilities_stack(stack):
                raise HTTPException(400, "Capabilities cells are not removed; move the cell off Capabilities first")
            if str(cell.id) in running_cell_ids():
                raise HTTPException(409, "Cell is running — stop the run first")
            current_tags = list(cell.tags)
        overrides = None
        if action is not None:
            try:
                overrides = {"tags": apply_tag_action(current_tags, action.add_tags, action.remove_tags)}
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        from cell_metadata import advance_cell_metadata_generations
        advance_cell_metadata_generations(channel_db_name(ch), [str(cell.id)])
        remember_removed_cells(channel_db_name(ch), stack, [cell], metadata_overrides=overrides)
        with state_lock:
            if require_channel(channel) is not ch or not any(s is stack for s in ch.main_stacks):
                raise HTTPException(409, "The stack changed while the cell was being removed")
            stack.cells.pop(next(i for i, c in enumerate(stack.cells) if c is cell))
            if not stack.cells:
                _pop_main_stack(ch, stack)
            return channel_layout(ch)

    stack_id = await asyncio.to_thread(_stack_id)
    result = await asyncio.to_thread(_with_stack_cells_locked, channel, stack_id, _body, hold_state=False)
    from state import flush_persist_state
    await asyncio.to_thread(persist_state)
    await asyncio.to_thread(flush_persist_state)
    announce_lineage(channel, [cell_id])
    return result


@router.post("/api/stacks/{channel}/{stack_id}/minimize")
async def minimize_stack(channel: str, stack_id: UUID):
    """Hide Capabilities to its top bar pill: the stack moves from
    main_stacks to nav_stacks. Any other stack is refused (400); a board
    stack is removed instead.
    """
    def _body():
        ch = require_channel(channel)
        with state_lock:
            stack = next((s for s in ch.main_stacks if s.id == stack_id), None)
            if stack is None:
                return channel_layout(ch)
            if not is_capabilities_stack(stack):
                raise HTTPException(400, "Only Capabilities is hidden to the top bar")
            _pop_main_stack(ch, stack)
            ch.nav_stacks.append(stack)
        persist_state()
        return channel_layout(ch)
    return await asyncio.to_thread(_body)


@router.post("/api/stacks/{channel}/{stack_id}/restore_minimized")
async def restore_minimized_stack(channel: str, stack_id: UUID, after: Optional[UUID] = None):
    """Show the hidden Capabilities stack again: it moves from nav to main.

    `after` is the stack the client considers currently selected; the restored
    stack is placed immediately to its right. The server's `main_selection_id`
    can lag the client (selection changes don't always round-trip), so clients
    should pass their live selection to guarantee correct placement.
    """
    def _body():
        ch = require_channel(channel)
        with state_lock:
            idx = next((i for i, s in enumerate(ch.nav_stacks) if s.id == stack_id), None)
            if idx is None:
                return channel_layout(ch)
            stack = ch.nav_stacks.pop(idx)
            # Prefer client-provided anchor; fall back to server's main_selection_id.
            anchor = after if after else ch.main_selection_id
            insert_idx = len(ch.main_stacks)
            if anchor:
                sel_idx = next((i for i, s in enumerate(ch.main_stacks) if s.id == anchor), None)
                if sel_idx is not None:
                    insert_idx = sel_idx + 1
            ch.main_stacks.insert(insert_idx, stack)
            ch.main_selection_id = stack.id
        persist_state()
        return channel_layout(ch)
    return await asyncio.to_thread(_body)


@router.put("/api/stacks/{channel}/{stack_id}/rename")
async def rename_stack(channel: str, stack_id: UUID, name: str):
    def _body():
        ch = require_channel(channel)
        with state_lock:
            s = next((s for s in ch.main_stacks if s.id == stack_id), None)
            if not s:
                s = next((s for s in ch.nav_stacks if s.id == stack_id), None)
            if not s:
                return channel_layout(ch)
            if is_capabilities_stack(s):
                raise HTTPException(400, "Capabilities keeps its name")
            s.name = name
        persist_state()
        return channel_layout(ch)
    return await asyncio.to_thread(_body)


@router.post("/api/stacks/{channel}/{stack_id}/move_cell_to/{target_stack_index}/{mode}")
async def move_cell_to(channel: str, stack_id: UUID, target_stack_index: int, mode: str,
                       cell_id: UUID, tgt_cell_index: int = 0):
    """Move the cell ``cell_id`` of the main stack ``stack_id`` into the
    stack at ``target_stack_index`` (``mode='stack'``, at ``tgt_cell_index``)
    or into a new column there (``mode='insert'``).

    A cell moves between any two stacks; it enters Capabilities only while
    nothing runs on it and never before the built-in cards; Capabilities
    never empties away. A cell no longer in the source stack is a 409 (the
    client retries from the fresh layout); a built-in card never moves (400);
    a cell with a run held (active, stopping or starting) cannot enter
    Capabilities (409), checked under the source stack's cell locks, which a
    run start takes too. A cell dropped into its own stack as that stack's
    only cell stays where it is. A board stack whose only cell leaves for a new
    column IS that column, keeping its id, name and width; a cell leaving
    Capabilities for a column gets a fresh standard stack."""
    if mode not in ("stack", "insert"):
        raise HTTPException(400, "Unknown cell move mode")

    def _body():
        from run_registry import running_cell_ids

        ch = require_channel(channel)
        with state_lock:
            src_idx = next((i for i, s in enumerate(ch.main_stacks) if s.id == stack_id), None)
            if src_idx is None:
                return channel_layout(ch)
            src_stack = ch.main_stacks[src_idx]
            at = next((i for i, c in enumerate(src_stack.cells) if c.id == cell_id), None)
            if at is None:
                raise HTTPException(409, "The cell moved — try again")
            cell = src_stack.cells[at]
            if cell.builtin:
                raise HTTPException(400, BUILTIN_REFUSAL)

            src_capabilities = is_capabilities_stack(src_stack)
            # The destination stack, resolved before anything moves: the
            # index names it on the board as it is now.
            dest = (ch.main_stacks[target_stack_index]
                    if mode == "stack" and 0 <= target_stack_index < len(ch.main_stacks) else None)
            if dest is src_stack and len(src_stack.cells) == 1:
                return channel_layout(ch)
            if (dest is not None and is_capabilities_stack(dest) and not src_capabilities
                    and str(cell.id) in running_cell_ids()):
                raise HTTPException(409, f"Stop the run in ‘{cell.name}’ before moving it onto Capabilities")

            target = target_stack_index
            src_stack.cells.pop(at)
            emptied = not src_stack.cells and not src_capabilities
            if emptied:
                ch.main_stacks.pop(src_idx)
                if target > src_idx:
                    target -= 1

            def own_column() -> Stack:
                if emptied:
                    src_stack.cells = [cell]
                    return src_stack
                return Stack(name="", cells=[cell])

            if mode == "stack":
                if dest is not None:
                    safe_tgt_idx = max(builtin_head(dest), min(tgt_cell_index, len(dest.cells)))
                    dest.cells.insert(safe_tgt_idx, cell)
                    ch.main_selection_id = dest.id
                else:
                    column = own_column()
                    ch.main_stacks.append(column)
                    ch.main_selection_id = column.id
            else:  # mode == 'insert'
                column = own_column()
                safe_idx = max(0, min(target, len(ch.main_stacks)))
                ch.main_stacks.insert(safe_idx, column)
                ch.main_selection_id = column.id

        persist_state()
        return channel_layout(ch)
    return await asyncio.to_thread(_with_stack_cells_locked, channel, stack_id, _body)


@router.post("/api/stacks/{channel}/{stack_id}/move_stack_to/{target_index}/{mode}")
async def move_stack_to(channel: str, stack_id: UUID, target_index: int, mode: str):
    """Reposition a stack. `mode='insert'` drops it as a new column at
    target_index; `mode='stack'` merges its cells into the target stack
    (source's cells appended to the end of target's existing cells)
    and removes the source column.

    Merging with Capabilities on either side is a no-op: its cells move one
    at a time, each checked for a run, and it never empties away.
    """
    def _body():
        ch = require_channel(channel)
        with state_lock:
            try:
                src_idx = next(i for i, s in enumerate(ch.main_stacks) if s.id == stack_id)
            except StopIteration:
                return channel_layout(ch)

            src_stack = ch.main_stacks[src_idx]
            nonlocal_target = target_index

            if mode == 'stack':
                if 0 <= nonlocal_target < len(ch.main_stacks) and nonlocal_target != src_idx:
                    target_stack = ch.main_stacks[nonlocal_target]
                    if is_capabilities_stack(src_stack) or is_capabilities_stack(target_stack):
                        return channel_layout(ch)
                    # Append semantics: the dragged column lands under the
                    # target, so its cells come after the target's existing
                    # cells (the overview's drag-merge is a drop under by
                    # gesture).
                    target_stack.cells = target_stack.cells + src_stack.cells
                    ch.main_stacks.pop(src_idx)
                    ch.main_selection_id = target_stack.id
            else:  # mode == 'insert'
                ch.main_stacks.pop(src_idx)
                if nonlocal_target > src_idx:
                    nonlocal_target -= 1
                safe_idx = max(0, min(nonlocal_target, len(ch.main_stacks)))
                ch.main_stacks.insert(safe_idx, src_stack)
                ch.main_selection_id = src_stack.id

        persist_state()
        return channel_layout(ch)
    return await asyncio.to_thread(_body)


@router.post("/api/stacks/{channel}/{stack_id}/duplicate_cell_in_stack/{cell_index}")
async def duplicate_cell_in_stack(channel: str, stack_id: UUID, cell_index: int):
    """Duplicate the cell at ``cell_index`` INTO THE SAME stack, inserting the
    copy immediately after it. Powers Duplicate on a fanned-out stack's
    column: the copy lands in the adjacent column (same underlying stack)
    rather than in a brand-new stack (``POST /cells/{cell_id}/duplicate``
    handles new-stack duplication). The copy keeps binaries (by blob hash)
    and never carries a run folder (``_runs/``). Every copy is recorded in
    the lineage (op 'duplicate'). Capabilities is refused (400): its cells
    are duplicated into a new stack."""
    def _body():
        from cell_files import copy_vfiles, checkpoint_cells, record_duplicates, take_back_copies
        from state import channel_db_name, state_lock

        ch = require_channel(channel)
        s = next((stk for stk in ch.main_stacks if stk.id == stack_id), None)
        if s is None:
            raise HTTPException(404, "Stack not found")
        if is_capabilities_stack(s):
            raise HTTPException(400, "Capabilities cells are duplicated into a new stack")
        if not (0 <= cell_index < len(s.cells)):
            raise HTTPException(400, "cell_index out of range")
        original = s.cells[cell_index]
        # Pin the accepted source snapshot in history for copy lineage.
        channel_uuid = channel_db_name(ch)
        promoted = checkpoint_cells(channel_uuid, [str(original.id)], "duplicate", best_effort=True)
        with state_lock:
            at = next((i for i, c in enumerate(s.cells) if c.id == original.id), None)
            if at is None:
                raise HTTPException(409, "The cell moved while it was being duplicated")
            new_cell = Cell(
                name=f"{original.name} (Copy)",
                icon=original.icon,
                tags=list(original.tags),
                files=copy_vfiles(original),
            )
            selection = ch.main_selection_id
            s.cells.insert(at + 1, new_cell)
            ch.main_selection_id = s.id
        try:
            record_duplicates(channel_uuid, [(str(original.id), str(new_cell.id))], promoted)
        except BaseException:
            take_back_copies(ch, selection, cells=[new_cell])
            raise
        persist_state()
        announce_lineage(channel, [original.id, new_cell.id])
        return stripped_channel(ch) | {"duplicated_cell_ids": [str(new_cell.id)]}
    result = await asyncio.to_thread(_body)
    from state import flush_persist_state
    await asyncio.to_thread(flush_persist_state)
    return result


@router.post("/api/stacks/{channel}/{stack_id}/duplicate_stack")
async def duplicate_stack(channel: str, stack_id: UUID):
    """Copy every cell of the given stack into a fresh stack placed one slot
    to the right — the whole-column duplicate behind the stack menu's
    Duplicate item and the duplicate-stack shortcut. New cells get fresh ids
    so the copy is independent of the original; files keep their blob hash
    and run folders (``_runs/``) never cross. Capabilities is refused (400):
    one of its cells is duplicated instead. One lineage row per copied cell
    (op 'duplicate', one op for the stack)."""
    def _body():
        from cell_files import copy_vfiles, checkpoint_cells, record_duplicates, take_back_copies
        from state import channel_db_name

        ch = require_channel(channel)
        src0 = next((stk for stk in ch.main_stacks if stk.id == stack_id), None)
        if src0 is None:
            raise HTTPException(404, "Stack not found")
        if is_capabilities_stack(src0):
            raise HTTPException(400, "Capabilities is not duplicated; duplicate one of its cells")
        # Pin each accepted source snapshot in history for copy lineage.
        channel_uuid = channel_db_name(ch)
        promoted = checkpoint_cells(channel_uuid, [str(c.id) for c in src0.cells],
                                    "duplicate", best_effort=True)
        with state_lock:
            s_idx = next((i for i, stk in enumerate(ch.main_stacks) if stk.id == stack_id), None)
            if s_idx is None:
                raise HTTPException(404, "Stack not found")
            src = ch.main_stacks[s_idx]
            originals = list(src.cells)
            new_cells = [
                Cell(name=f"{c.name} (Copy)", icon=c.icon, tags=list(c.tags), files=copy_vfiles(c))
                for c in originals
            ]
            new_stack = Stack(name=src.name, cells=new_cells)
            selection = ch.main_selection_id
            ch.main_stacks.insert(s_idx + 1, new_stack)
            ch.main_selection_id = new_stack.id
            pairs = [(str(c.id), str(copy.id)) for c, copy in zip(originals, new_cells)]
        if pairs:
            try:
                record_duplicates(channel_uuid, pairs, promoted)
            except BaseException:
                take_back_copies(ch, selection, stack=new_stack)
                raise
        persist_state()
        announce_lineage(channel, [cell_id for pair in pairs for cell_id in pair])
        return stripped_channel(ch)
    result = await asyncio.to_thread(_body)
    from state import flush_persist_state
    await asyncio.to_thread(flush_persist_state)
    return result

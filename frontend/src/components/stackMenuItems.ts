import { Pencil, UnfoldHorizontal, FoldHorizontal, CopyPlus, FoldVertical, Minus, ArrowUp, Settings, PackagePlus, Sparkles } from 'lucide-react';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import type { MenuEntry } from '@/components/ui/Menu';

interface StackMenuPolicy {
    cellCount: number;
    hasLiveRun: boolean;
    /** The stack is fanned out: the menu sits on its bracket and offers Collapse. */
    fanned?: boolean;
    onRename?: () => void;
    /** Fan out, or Collapse while `fanned`. */
    onToggleFan?: () => void;
    onDuplicateStack?: () => void;
    onMergeStack?: () => void;
    /** Remove the stack (it plays the stack burn). */
    onRemove: () => void;
}

export const STACK_RUNNING_REASON = 'A run is live in this stack — stop it first';
export const STACK_SINGLE_CELL_REASON = "Single cell — use the cell's own Duplicate / remove controls";
/** Hide's title on the Capabilities menu and in its cells' heads. */
export const CAPABILITIES_HIDE_HINT = 'Hide Capabilities to the top bar';

/** A standard stack's menu rows, top to bottom (C07): Rename, Fan out /
 *  Collapse, Duplicate, Merge, a separator, Remove. On a single-cell stack
 *  Fan out / Duplicate / Merge / Remove are inert with their reasons (a
 *  cell's own header carries its duplicate and remove). A live run greys
 *  Merge and Remove (a run's files are its lock); Duplicate stays live,
 *  since the copy leaves the runs' files behind. Inert rows stay in the
 *  list, their reason as the title. Split is not here: it is a
 *  Commands-pane command (D-C18f). */
export function buildStackMenuItems({
    cellCount, hasLiveRun, fanned = false,
    onRename, onToggleFan, onDuplicateStack, onMergeStack, onRemove,
}: StackMenuPolicy): MenuEntry[] {
    const single = cellCount < 2;
    const items: MenuEntry[] = [];
    if (onRename) {
        items.push({
            kind: 'item', id: 'rename', label: 'Rename', icon: Pencil, hint: 'Rename this stack',
            onSelect: onRename,
        });
    }
    if (onToggleFan) {
        items.push(fanned
            ? {
                kind: 'item', id: 'collapse', label: 'Collapse', icon: FoldHorizontal,
                shortcut: SHORTCUT_IDS.SCENE_FAN_OUT,
                hint: 'Collapse — put the cells back into one stack',
                onSelect: onToggleFan,
            }
            : {
                kind: 'item', id: 'fan-out', label: 'Fan out', icon: UnfoldHorizontal,
                shortcut: SHORTCUT_IDS.SCENE_FAN_OUT,
                hint: 'Fan out — show every cell side by side',
                disabled: single, reason: 'Fan out needs at least 2 cells',
                onSelect: onToggleFan,
            });
    }
    if (onDuplicateStack) {
        items.push({
            kind: 'item', id: 'duplicate', label: 'Duplicate', icon: CopyPlus,
            shortcut: SHORTCUT_IDS.STACK_DUPLICATE_STACK,
            hint: 'Duplicate this stack',
            disabled: single, reason: STACK_SINGLE_CELL_REASON,
            onSelect: onDuplicateStack,
        });
    }
    if (onMergeStack) {
        items.push({
            kind: 'item', id: 'merge', label: 'Merge', icon: FoldVertical,
            hint: 'Merge — combine this stack into a single cell',
            disabled: single || hasLiveRun,
            reason: single ? 'Merge needs a stack with 2+ cells' : STACK_RUNNING_REASON,
            onSelect: onMergeStack,
        });
    }
    if (items.length > 0) items.push({ kind: 'separator', id: 'sep-remove' });
    items.push({
        kind: 'item', id: 'remove', label: 'Remove', icon: Minus, danger: true,
        shortcut: SHORTCUT_IDS.STACK_REMOVE,
        hint: 'Remove this stack from the canvas (its cells go to the Removed shelf)',
        disabled: single || hasLiveRun,
        reason: single ? STACK_SINGLE_CELL_REASON : STACK_RUNNING_REASON,
        onSelect: onRemove,
    });
    return items;
}

interface CapabilitiesMenuActions {
    /** Front the manage card (the stack's slot 0). */
    onManage: () => void;
    /** The manage card's 'Add from catalog' step. */
    onAddFromCatalog: () => void;
    /** The manage card's Create questionnaire. */
    onCreate: () => void;
    /** Hide the stack to its top-bar pill (Remove's slot). */
    onHide: () => void;
}

/** The Capabilities stack's menu: Manage, Add from catalog…, Create a
 *  capability…, a separator, Hide. It keeps its name and its place as one
 *  stack: no Rename, Fan out, Duplicate or Merge, and it is hidden, never
 *  removed. Nothing runs on it, so no row waits on a run. */
export function buildCapabilitiesMenuItems({
    onManage, onAddFromCatalog, onCreate, onHide,
}: CapabilitiesMenuActions): MenuEntry[] {
    return [
        {
            kind: 'item', id: 'manage', label: 'Manage', icon: Settings,
            hint: 'List and edit every capability on this stack',
            onSelect: onManage,
        },
        {
            kind: 'item', id: 'add-from-catalog', label: 'Add from catalog…', icon: PackagePlus,
            hint: 'Add a cell from the suggested catalog',
            onSelect: onAddFromCatalog,
        },
        {
            kind: 'item', id: 'create', label: 'Create a capability…', icon: Sparkles,
            hint: 'Answer a few questions, then make it in a work stack',
            onSelect: onCreate,
        },
        { kind: 'separator', id: 'sep-hide' },
        {
            kind: 'item', id: 'hide', label: 'Hide', icon: ArrowUp,
            shortcut: SHORTCUT_IDS.STACK_REMOVE,
            hint: CAPABILITIES_HIDE_HINT,
            onSelect: onHide,
        },
    ];
}

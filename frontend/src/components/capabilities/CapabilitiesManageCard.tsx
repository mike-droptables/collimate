import { useRef } from 'react';
import { ArrowUp, Settings } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { BARE_KEYS_OWNER_PROPS } from '@/lib/keyboardOwner';
import { isFocusWheel } from '@/lib/wheelReplay';
import { cn } from '@/lib/utils';
import { StackWidthControl } from '@/components/StackWidthControl';
import { iconBtn } from '@/components/ui/formStyles';
import { CapabilitiesManageBody } from './CapabilitiesManageBody';
import { SELECTED_EDGE_STYLE } from '@/lib/selectionEdge';

/** The Capabilities stack's slot 0 (StackGroup renders it in the card
 *  position, under the same motion layers as a cell card). */
export interface CapabilitiesManageCardProps {
    stackId: string;
    /** The card's size (its motion layer sizes it; the card fills it). */
    width: number;
    height: number;
    /** The Capabilities stack is the selected stack. */
    isSelected: boolean;
    /** The manage card is the stack's front face. */
    isFront: boolean;
    /** A press on the card selects the stack. */
    onSelect(): void;
    /** Bring this Capabilities cell to the front of the stack. */
    onShowCell(cellId: string): void;
    /** Hide Capabilities to its top-bar pill. */
    onHide(): void;
}

/**
 * The manage view: slot 0 of the Capabilities stack, before the built-in
 * cards and the user's cells. It lists every generator, runner, renderer
 * and handler of the channel and hosts their editors (the body,
 * CapabilitiesManageBody, is also every Capabilities cell's Manage tab,
 * scoped to that cell); nothing about it is sent to the server, and it is
 * never dragged.
 *
 * Its root owns its bare keys (BARE_KEYS_OWNER_PROPS): '/' typed in its
 * search never opens Commands, while the scene's chords (Alt+↑/↓ paging,
 * Ctrl+←/→, Mod+K, Alt+Shift+Backspace hiding the stack) still work from
 * it. Fronted on the selected stack, it focuses its search once nothing
 * holds the keyboard (the scene's hand-off follows the selected cell, and
 * the manage card is none; keys left in another cell are released a frame
 * later, so it looks again for two frames). A vertical wheel over it
 * selects the stack and scrolls the list, never the stack.
 */
export function CapabilitiesManageCard({ stackId, isSelected, isFront, onSelect, onShowCell, onHide }: CapabilitiesManageCardProps) {
    const channel = useCollimateStore(s => s.activeChannel?.id ?? '');
    const rootRef = useRef<HTMLDivElement>(null);
    return <div ref={rootRef} {...BARE_KEYS_OWNER_PROPS} data-capabilities-manage="" data-stack-card="" tabIndex={-1}
        aria-label="Manage Capabilities"
        style={{ width: '100%', height: '100%', pointerEvents: 'auto', backgroundColor: isFront ? 'var(--sheet)' : 'var(--canvas)', ...(isSelected && isFront ? SELECTED_EDGE_STYLE : null) }}
        className={cn('group/card relative flex flex-col overflow-hidden rounded-lg border outline-none',
            isSelected && isFront ? 'border-brass' : 'border-line', isFront && 'shadow-(--shadow)')}
        onPointerDown={e => { e.stopPropagation(); if (e.button !== 2) onSelect(); }}
        onWheelCapture={e => { if (!isSelected && isFocusWheel(e)) onSelect(); }}>
        <div className={cn('relative flex h-9 flex-none items-center gap-[0.625rem] border-b border-line pl-[0.875rem] pr-[0.625rem]', isFront && 'bg-teal-sheet')}>
            <Settings className="size-3.5 flex-none text-teal" strokeWidth={1.6} aria-hidden />
            <span className={cn('min-w-0 flex-1 truncate text-[0.875rem] font-semibold tracking-[-0.005em]', isFront ? 'text-ink' : 'text-ink-2')}>Manage</span>
            {isFront && <div className="flex flex-none items-center gap-1">
                <StackWidthControl stackId={stackId} onDragStart={onSelect} />
                <button type="button" aria-label="Hide Capabilities" title="Hide Capabilities to the top bar" data-testid="manage-hide-stack"
                    className={cn(iconBtn, 'h-6 w-6')} onClick={e => { e.stopPropagation(); onHide(); }}>
                    <ArrowUp className="h-3.5 w-3.5" strokeWidth={1.6} aria-hidden />
                </button>
            </div>}
        </div>
        <CapabilitiesManageBody key={channel} active={isSelected && isFront} rootRef={rootRef} onShowCell={onShowCell} />
    </div>;
}

import { useState } from 'react';
import type { HandlerStep } from '@/stores/collimate/selectionSlice';
import type { HandlerInfo } from '@/lib/definitionApi';
import { HANDLER_MODEL } from '@/lib/handlerStatus';
import { BuiltinDisclosure } from '@/components/capabilities/CapabilityGroup';
import { HandlerCard } from './HandlerCard';

/** Past this many handlers the cards start folded to name and status. */
const UNFOLDED_MAX = 3;

/** A request to show one handler (matched on its cell and folder): each
 *  new `seq` is shown once; the holder drops it once the card reports it
 *  shown (`onRevealed`). */
export interface HandlerFocus { seq: number; cellId: string; folder: string; step?: HandlerStep }

const matches = (focus: HandlerFocus | null, handler: HandlerInfo) =>
    !!focus && focus.cellId === handler.cell_id && focus.folder === handler.folder;

/**
 * The Handlers group of the Capabilities manage view: the one-line model of
 * what a handler is, then one card per handler of the channel (HandlerCard),
 * the user's first and the app's in a 'Built-in (n)' disclosure. A focus
 * (from the store's capabilitiesManageRequest) unfolds the disclosure that
 * holds its handler and hands the card its step.
 */
export function HandlerManagement({ handlers, builtins, filtering, focus, onRevealed, onEdit, onShowCell }: {
    handlers: readonly HandlerInfo[];
    builtins: readonly HandlerInfo[];
    /** A search narrows the list: the built-ins it matches show unfolded. */
    filtering: boolean;
    focus: HandlerFocus | null;
    /** The matching card has shown focus `seq`. */
    onRevealed: (seq: number) => void;
    onEdit: (handler: HandlerInfo, saved: (notice: string) => void) => void;
    onShowCell: (cellId: string) => void;
}) {
    const [builtinOpen, setBuiltinOpen] = useState(false);
    const [opened, setOpened] = useState<number | null>(null);
    if (focus && focus.seq !== opened && builtins.some(handler => matches(focus, handler))) {
        setOpened(focus.seq);
        setBuiltinOpen(true);
    }
    const collapsible = handlers.length + builtins.length > UNFOLDED_MAX;
    const card = (handler: HandlerInfo) => <HandlerCard key={`${handler.cell_id}:${handler.folder}`} handler={handler}
        collapsible={collapsible} onEdit={onEdit} onShowCell={onShowCell} onRevealed={onRevealed}
        focus={matches(focus, handler) ? { seq: focus!.seq, step: focus!.step } : undefined} />;
    return <div className="flex flex-col gap-2.5">
        <p data-handler-model="" className="mx-0.5 text-[0.75rem] leading-[1.45] text-ink-3">{HANDLER_MODEL}</p>
        {handlers.length > 0 && <div data-handler-list="" className="flex flex-col gap-2.5">{handlers.map(card)}</div>}
        <BuiltinDisclosure count={builtins.length} open={builtinOpen || filtering} onToggle={() => setBuiltinOpen(value => !value)}>
            <div data-handler-list="builtin" className="flex flex-col gap-2.5">{builtins.map(card)}</div>
        </BuiltinDisclosure>
    </div>;
}

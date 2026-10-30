import { useState } from 'react';
import { Switch } from '@/components/ui/switch';
import { EditableLabel } from '@/components/ui/editable-label';
import { btn, chip, formLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import type { RendererEntry } from '@/lib/api';

interface RendererCardProps {
    renderer: RendererEntry;
    /** Current channel renderer preferences: { extension: renderer id }. */
    prefs: Record<string, string>;
    onToggleAvailable: (enabled: boolean) => void;
    onSetDefault: (ext: string) => void;
    /** Rewrite the `.renderer` file's name or summary in the cell that
     *  holds it (the host's per-row writer). */
    onEditDisplay: (patch: { name?: string; summary?: string }) => void;
    /** Bring the cell that holds it (a built-in's card) to the front. */
    onOpenCell?: () => void;
}

const DEFAULT_CHIP = 'inline-flex h-5 items-center rounded-full border px-2 font-mono text-[0.6875rem] transition-colors';

/**
 * One renderer's row in the Capabilities manage view's Renderers group (the
 * mock's `.rrow`): its types in
 * mono over its name, the availability switch in the second column and the
 * description under both, between hairline rules. A click on the row (not
 * the switch) opens its detail under it: the per-extension default chips
 * (the default is the chip's teal on-state) and the name and summary
 * editors, which rewrite the `.renderer` file, and 'Open cell' ('Open card'
 * for a built-in). A built-in renderer is always available and read-only:
 * a "Built-in" label in the switch's place, its name and summary plain;
 * its default chips stay (they are channel preferences).
 */
export function RendererCard({ renderer, prefs, onToggleAvailable, onSetDefault, onEditDisplay, onOpenCell }: RendererCardProps) {
    const [open, setOpen] = useState(false);
    const available = renderer.available !== false;
    const exts: string[] = renderer.extensions ?? [];
    // A chip is "default" only when an explicit preference points at this
    // renderer by id (not the implicit first-match winner).
    const isDefault = (ext: string) => prefs[ext] === renderer.id;
    const types = exts.length ? exts.join(' ') : renderer.id;

    return (
        <div data-renderer-row={renderer.id} className="border-t border-line last:border-b">
            <div
                className="grid cursor-pointer grid-cols-[1fr_auto] gap-x-2.5 gap-y-0.5 px-2 py-3"
                onClick={() => setOpen(v => !v)}
            >
                <button
                    type="button"
                    aria-expanded={open}
                    aria-label={`${types} ${renderer.name}`}
                    className="col-start-1 row-span-2 row-start-1 flex min-w-0 flex-col items-start text-left outline-none focus-visible:underline"
                >
                    <span className="max-w-full truncate font-mono text-[0.75rem] text-ink-2">{types}</span>
                    <span className="mt-px max-w-full truncate font-semibold text-ink">{renderer.name}</span>
                </button>
                {renderer.builtin
                    ? <span className={cn(chip, 'col-start-2 row-span-2 row-start-1 mt-1 self-start')} title="Part of the app: always available, read-only">Built-in</span>
                    : <Switch
                        checked={available}
                        onClick={e => e.stopPropagation()}
                        onCheckedChange={v => onToggleAvailable(v)}
                        aria-label={`${renderer.name} available`}
                        title={available ? 'Renderer available — click to disable' : 'Renderer disabled — click to enable'}
                        className="col-start-2 row-span-2 row-start-1 mt-1 self-start"
                    />}
                {renderer.summary && <p className="col-span-2 mt-0.5 text-[0.78125rem] leading-[1.45] text-ink-2">{renderer.summary}</p>}
            </div>
            {open && <div data-renderer-detail="" className="flex flex-col gap-2.5 px-2 pb-3">
                {exts.length > 0 && <div className="flex flex-col gap-1">
                    <span className={formLabel}>Default for</span>
                    <div className="flex flex-wrap gap-1">
                        {exts.map(ext => {
                            const isDef = isDefault(ext);
                            return (
                                <button
                                    key={ext}
                                    type="button"
                                    disabled={!available || isDef}
                                    aria-pressed={isDef}
                                    aria-label={`Default renderer for ${ext}`}
                                    onClick={() => { if (available && !isDef) onSetDefault(ext); }}
                                    title={
                                        !available
                                            ? 'Enable the renderer to set it as default'
                                            : isDef
                                                ? `Default renderer for ${ext}`
                                                : `Set as default renderer for ${ext}`
                                    }
                                    className={cn(
                                        DEFAULT_CHIP,
                                        isDef
                                            ? 'border-teal bg-teal-soft text-teal'
                                            : available
                                                ? 'cursor-pointer border-line-2 text-ink-2 hover:bg-sheet-2 hover:text-ink'
                                                : 'cursor-not-allowed border-line text-ink-3',
                                    )}
                                >
                                    {ext}
                                </button>
                            );
                        })}
                    </div>
                </div>}
                {!renderer.builtin && <div className="flex flex-col gap-1">
                    <span className={formLabel}>Name</span>
                    <EditableLabel
                        value={renderer.name}
                        onSave={name => { if (name) onEditDisplay({ name }); }}
                        className="text-[0.8125rem] font-semibold text-ink"
                        inputClassName="text-[0.8125rem]"
                    />
                </div>}
                {!renderer.builtin && <div className="flex flex-col gap-1">
                    <span className={formLabel}>Summary</span>
                    <EditableLabel
                        value={renderer.summary || ''}
                        onSave={summary => onEditDisplay({ summary })}
                        className="text-[0.78125rem] leading-[1.45] text-ink-2"
                        inputClassName="text-[0.78125rem]"
                        placeholder="Add a description…"
                        multiline
                    />
                </div>}
                {onOpenCell && <button type="button" className={`${btn} self-start`} onClick={onOpenCell}>{renderer.builtin ? 'Open card' : 'Open cell'}</button>}
            </div>}
        </div>
    );
}

// A live run's options (gen.publish_options, read from its /runs/active
// row), at the foot of its drilldown: one compact row per option, a switch
// for a boolean and a select for a select. A change is sent to the run (PUT
// …/runs/{run_id}/options) and shows as pending — the picked value, its
// control held — until the run answers it with a published set, which
// settles it to the value now in effect, whether the run applied the change
// or not.

import { useId, useRef, useState } from 'react';
import { cn } from '@/lib/utils';
import { describeApiError } from '@/lib/api/client';
import { putRunOption, type RunOption } from '@/lib/api';

type Value = string | boolean;

export interface RunOptionsProps {
    channel: string;
    runId: string;
    /** The run's published set and its revision (the row's `options_rev`). */
    options: readonly RunOption[];
    rev: number;
}

/** A change sent at the set's revision `rev`, the `seq`-th this footer sent. */
interface Pending { value: Value; rev: number; seq: number }

/** Whether `option` takes `value` (the host's own check on a change). */
const takes = (option: RunOption, value: Value): boolean =>
    option.type === 'boolean' ? typeof value === 'boolean' : option.options.some(choice => choice.value === value);

/** The changes still waiting for the run's answer, by option id. The run
 *  answers its changes one at a time in the order they were sent, each with
 *  a published set, so a change is answered by the first set after both its
 *  own send and the answer to the change ahead of it. A change already in
 *  effect is settled; one the latest set no longer takes was dropped by the
 *  host unanswered, so it is settled and holds no answer. */
function waitingChanges(pending: Record<string, Pending>, options: readonly RunOption[], rev: number): Map<string, Value> {
    const held = new Map<string, Value>();
    let answeredAt = 0;
    for (const [id, entry] of Object.entries(pending).sort(([, a], [, b]) => a.seq - b.seq)) {
        const option = options.find(o => o.id === id);
        if (!option || !takes(option, entry.value)) continue;
        answeredAt = Math.max(entry.rev, answeredAt) + 1;
        if (rev < answeredAt && option.currentValue !== entry.value) held.set(id, entry.value);
    }
    return held;
}

export function RunOptions({ channel, runId, options, rev }: RunOptionsProps) {
    const [pending, setPending] = useState<Record<string, Pending>>({});
    const [error, setError] = useState<string | null>(null);
    const sent = useRef(0);
    if (!options.length) return null;

    const held = waitingChanges(pending, options, rev);
    const change = (option: RunOption, value: Value) => {
        setError(null);
        sent.current += 1;
        setPending(prev => ({ ...prev, [option.id]: { value, rev, seq: sent.current } }));
        putRunOption(channel, runId, option.id, value).catch((err: unknown) => {
            setPending(prev => {
                const { [option.id]: _dropped, ...rest } = prev;
                return rest;
            });
            setError(`${option.name}: ${describeApiError(err)}`);
        });
    };

    return (
        <div data-testid="run-options" className="flex-none flex flex-col gap-px px-3 py-1 border-t border-line bg-sheet">
            {options.map(option => {
                const waiting = held.has(option.id);
                return (
                    <OptionRow
                        key={option.id}
                        option={option}
                        value={waiting ? held.get(option.id)! : option.currentValue}
                        pending={waiting}
                        onChange={value => change(option, value)}
                    />
                );
            })}
            {error && <div data-testid="run-options-error" role="status" className="text-[0.6875rem] text-red truncate" title={error}>{error}</div>}
        </div>
    );
}

function OptionRow({ option, value, pending, onChange }: {
    option: RunOption;
    value: Value;
    pending: boolean;
    onChange: (value: Value) => void;
}) {
    const id = useId();
    return (
        <div data-testid={`run-option-${option.id}`} data-pending={pending || undefined}
            className="flex items-center gap-2 h-6 min-w-0 text-[0.75rem]">
            <label htmlFor={id} title={option.description || option.name} className="flex-1 min-w-0 truncate text-ink-2">
                {option.name}
            </label>
            {option.type === 'boolean' ? (
                <button
                    id={id}
                    type="button"
                    role="switch"
                    aria-checked={value === true}
                    disabled={pending}
                    title={option.description || undefined}
                    onClick={() => onChange(value !== true)}
                    className={cn('relative flex-none w-6 h-3.5 rounded-full border transition-colors disabled:opacity-60',
                        value === true ? 'bg-teal border-teal' : 'bg-sheet-2 border-line-2')}
                >
                    <i aria-hidden className={cn('absolute top-px w-2.5 h-2.5 rounded-full bg-sheet transition-[left]',
                        value === true ? 'left-[0.6875rem]' : 'left-px')} />
                </button>
            ) : (
                <select
                    id={id}
                    value={String(value)}
                    disabled={pending}
                    onChange={e => { if (e.target.value !== value) onChange(e.target.value); }}
                    className="flex-none max-w-[12rem] h-5 px-1 rounded-sm border border-line-2 bg-sheet text-[0.75rem] text-ink outline-none focus:border-ink-3 disabled:opacity-60 cursor-pointer"
                >
                    {/* A value in effect that the set does not list (a model
                        the harness picked by its own name) still shows. */}
                    {!option.options.some(choice => choice.value === value) && <option value={String(value)}>{String(value)}</option>}
                    {option.options.map(choice => (
                        <option key={choice.value} value={choice.value} title={choice.description}>{choice.name}</option>
                    ))}
                </select>
            )}
        </div>
    );
}

import { useCellSelected } from '@/contexts/CellSelectionContext';
import { modeTab, modeTabOff, modeTabOn } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import type { RunsPaneOption } from '@/stores/collimate/types';

const OPTIONS: ReadonlyArray<{ id: RunsPaneOption; label: string }> = [
    { id: 'default', label: 'Default' },
    { id: 'file', label: 'Current file' },
    { id: 'log', label: 'Log' },
];

export interface PaneOptionsProps {
    testIdPrefix: 'drilldown-option' | 'all-option';
    value: RunsPaneOption;
    onChange: (option: RunsPaneOption) => void;
    /** The cell's last viewed file (Current file's title). */
    fileLabel: string | null;
    /** The tab panel the options control. */
    panelId: string;
}

/** A drilldown's or All's options: Default (the messages with the files
 *  touched), Current file (the same about the file last viewed in the
 *  cell) and Log. The rule is brass in the selected cell, teal elsewhere. */
export function PaneOptions({ testIdPrefix, value, onChange, fileLabel, panelId }: PaneOptionsProps) {
    const selected = useCellSelected();
    return (
        <div role="tablist" aria-label="Show" className="flex-none h-7 flex items-end gap-0.5 px-1.5 border-b border-line select-none">
            {OPTIONS.map(option => {
                const on = option.id === value;
                return (
                    <button
                        key={option.id}
                        type="button"
                        role="tab"
                        data-testid={`${testIdPrefix}-${option.id}`}
                        aria-selected={on}
                        aria-controls={on ? panelId : undefined}
                        title={option.id === 'file' ? fileLabel ?? 'No file is open in this cell' : undefined}
                        onClick={() => onChange(option.id)}
                        className={cn(modeTab, on ? modeTabOn : modeTabOff, on && (selected ? 'after:bg-brass' : 'after:bg-teal'),
                            'after:top-auto after:-bottom-px')}
                    >
                        {option.label}
                    </button>
                );
            })}
        </div>
    );
}

import { useState, type DragEvent as ReactDragEvent, type ReactNode } from 'react';
import { captureDrop, collectDroppedFiles, hasDroppedItems } from '@/lib/dropEntries';
import { carriesOsFiles } from '@/lib/fileDrag';
import { btn, card, formSelect, sectionLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import type { ChannelChoice, PickedFile, SourceMode } from './state';

const MODES: { mode: SourceMode; label: string }[] = [
    { mode: 'none', label: 'None' },
    { mode: 'channel', label: 'From channel' },
    { mode: 'file', label: 'From file' },
];

interface Props {
    /** The section's name, its radio group's and its controls' labels. */
    label: string;
    mode: SourceMode;
    onMode: (mode: SourceMode) => void;
    channels: ChannelChoice[];
    channelId: string;
    onChannel: (id: string) => void;
    /** What the picked channel brings (a listing, an export link). */
    channelBody?: ReactNode;
    /** The file the section read, or null before one is chosen. */
    file: PickedFile<unknown> | null;
    /** The picker button's label: `Choose zip…` or `Choose JSON…`. */
    pickLabel: string;
    /** Open the picker. Called straight from the click, so the picker
     *  opens inside the gesture. */
    onPick: () => void;
    /** Files dropped on the section's drop zone. */
    onDrop: (files: File[]) => void;
    /** Below the file line (a tag field). */
    fileBody?: ReactNode;
}

/** One Config section: None, a channel to copy from, or a file to read. */
export function SourceSection({ label, mode, onMode, channels, channelId, onChannel, channelBody, file, pickLabel, onPick, onDrop, fileBody }: Props) {
    const [over, setOver] = useState(false);
    const onDragOver = (e: ReactDragEvent) => {
        if (!carriesOsFiles(e.dataTransfer)) return;
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = 'copy';
        setOver(true);
    };
    const onDragLeave = (e: ReactDragEvent) => {
        if (!(e.currentTarget as Node).contains(e.relatedTarget as Node | null)) setOver(false);
    };
    const onDropped = (e: ReactDragEvent) => {
        if (!carriesOsFiles(e.dataTransfer)) return;
        e.preventDefault();
        e.stopPropagation();
        setOver(false);
        // dataTransfer.items dies when this handler returns: snapshot now.
        const capture = captureDrop(e.dataTransfer);
        if (!hasDroppedItems(capture)) return;
        void collectDroppedFiles(capture).then(drop => onDrop(drop.files.map(entry => entry.file)));
    };
    return <section aria-label={label} className={`${card} flex flex-col gap-2 p-3`}>
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
            <h3 className={sectionLabel}>{label}</h3>
            <div role="radiogroup" aria-label={label} className="flex flex-wrap gap-3">
                {MODES.map(choice => {
                    const disabled = choice.mode === 'channel' && !channels.length;
                    return <label key={choice.mode} title={disabled ? 'No channel to copy from' : undefined}
                        className={cn('flex items-center gap-1.5 text-[0.78125rem]', disabled ? 'text-ink-3' : 'cursor-pointer text-ink-2')}>
                        <input type="radio" className="accent-teal" name={label} checked={mode === choice.mode} disabled={disabled} onChange={() => onMode(choice.mode)} />
                        <span className={mode === choice.mode ? 'text-ink' : undefined}>{choice.label}</span>
                    </label>;
                })}
            </div>
        </div>
        {mode === 'channel' && <div className="flex flex-col gap-2">
            <select aria-label={`${label} channel`} className={formSelect} value={channelId} onChange={e => onChannel(e.target.value)}>
                {channels.map(channel => <option key={channel.id} value={channel.id}>{channel.name || 'Untitled channel'}</option>)}
            </select>
            {channelBody}
        </div>}
        {mode === 'file' && <div data-testid={`${label} drop zone`} onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDropped}
            className={cn('flex flex-col gap-1.5 rounded-md border border-dashed px-3 py-2.5 transition-colors', over ? 'border-teal bg-teal-soft' : 'border-line-2')}>
            <div className="flex flex-wrap items-center gap-2">
                <button type="button" className={btn} onClick={onPick}>{pickLabel}</button>
                <span className="text-[0.75rem] text-ink-3">or drop it here</span>
            </div>
            {file && <FileLine file={file} />}
            {fileBody}
        </div>}
    </section>;
}

function FileLine({ file }: { file: PickedFile<unknown> }) {
    if (file.status === 'reading') return <p role="status" className="text-[0.75rem] text-ink-2">Uploading {file.name}…</p>;
    if (file.status === 'refused') return <p role="alert" className="text-[0.75rem] text-red">{file.name ? `${file.name}: ` : ''}{file.error}</p>;
    return <p className="text-[0.75rem] text-ink-2"><span className="text-ink">{file.name}</span> · {file.summary}</p>;
}

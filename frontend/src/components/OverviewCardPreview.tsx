import {useFileBodies} from '@/hooks/useFileBodies';
import {currentChannelId} from '@/lib/currentChannel';
import React from 'react';
import { Clock, File } from 'lucide-react';
import type { Cell, VirtualFile } from '@/lib/types';
import { isRunPath } from '@/lib/runningCell';
import { activeTab } from '@/lib/cellLayout';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { FileRows } from './overview/journalGlyphs';
import { baseName } from './overview/journalText';

// The body of an overview card at rest (the mock's `.ov-body` from
// cardHtml): the meta row, the cell's files, a rule, then the open file's
// name and the first lines of its source. Everything is read from the cell
// the store already holds, never fetched: the preview reads at most
// PREVIEW_CHARS of one file, so a large cell costs what a small one does.

const BINARY_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.bmp', '.pdf', '.zip', '.tar', '.gz', '.bin', '.exe', '.dll', '.so', '.dylib', '.mp3', '.mp4', '.wav', '.ogg', '.woff', '.woff2', '.ttf', '.otf'];
/** The preview's bound: the first PREVIEW_LINES lines of the file's first
 *  PREVIEW_CHARS characters (the server's shelf preview reads 4 KB). */
const PREVIEW_CHARS = 4096;
const PREVIEW_LINES = 14;
/** The files list names four files, or three and "+n more". */
const FILES_SHOWN = 4;

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad2 = (n: number) => String(n).padStart(2, '0');

/** A time in local time, the mock's way: "2 Oct 2026 · 09:12". Empty for
 *  a missing or unreadable time. */
function formatCardTime(iso: string | null | undefined): string {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()} · ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/** A file's base name split into its stem and its extension (as
 *  FileName shows it). */
function splitName(path: string): { stem: string; ext: string } {
    const base = baseName(path);
    const dot = base.lastIndexOf('.');
    return dot > 0 ? { stem: base.slice(0, dot), ext: base.slice(dot) } : { stem: base, ext: '' };
}

/** The cell's own files, run folders left out, by path. */
export const cardFiles = (cell: Pick<Cell, 'files'>): VirtualFile[] =>
    cell.files.filter(f => !isRunPath(f.name)).sort((a, b) => a.name.localeCompare(b.name));

const isTextFile = (f: VirtualFile) =>
    f.encoding!=='binary' && !BINARY_EXTS.some(ext => f.name.toLowerCase().endsWith(ext));

/** The file the card previews: the open file, else the pinned one, else the
 *  first text file; null when none of them is text. */
function previewFile(files: VirtualFile[], openPath: string | null, pinned: string | undefined): VirtualFile | null {
    for (const path of [openPath, pinned]) {
        const f = path ? files.find(x => x.name === path) : undefined;
        if (f && isTextFile(f)) return f;
    }
    return files.find(isTextFile) ?? null;
}

/** The first lines of a file's source, bounded (the mock's sourceOf). */
const previewSource = (content: string): string =>
    content.slice(0, PREVIEW_CHARS).split('\n').slice(0, PREVIEW_LINES).join('\n').trimEnd();

/** A section head inside a card (the mock's `.ov-h`). */
function CardSection({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
    return (
        <div className="mt-2.5 flex items-center gap-1.5 text-[0.6875rem] font-semibold uppercase tracking-[0.08em] text-ink-2 [&_svg]:h-3 [&_svg]:w-3 [&_svg]:text-ink-3">
            {icon}{children}
        </div>
    );
}

/** The meta row (the mock's `.ov-meta`): a clock, the time, the author at
 *  the right. */
function CardMeta({ time, by }: { time: string; by: string }) {
    return (
        <div className="flex items-center gap-1.5 whitespace-nowrap text-ink-3">
            <Clock className="h-3 w-3 flex-none" strokeWidth={1.6} />
            <span>{time}</span>
            <span className="ml-auto min-w-0 truncate">{by}</span>
        </div>
    );
}

export const OverviewCardPreview = React.memo(function OverviewCardPreview({ cell, isOffscreen, author }: {
    cell: Cell;
    /** Far outside the view: the files count only. */
    isOffscreen: boolean;
    /** Who the card names at the right of its meta row. */
    author: string;
}) {
    // The open file: the focused tab of the cell's layout, when it is a file.
    const openPath = useCollimateStore(s => {
        const layout = s.cellLayouts[cell.id];
        const tab = layout ? activeTab(layout) : null;
        return tab?.kind === 'file' ? tab.path : null;
    });
    const files = React.useMemo(() => cardFiles(cell), [cell]);
    const previewMetadata=React.useMemo(()=>previewFile(files,openPath,cell.pinned),[files,openPath,cell.pinned]);
    const {files:loaded,error}=useFileBodies(cell.channel||currentChannelId(),cell.id,previewMetadata?[previewMetadata]:[],undefined,!isOffscreen);
    const preview=previewMetadata?{file:previewMetadata,text:loaded?previewSource(loaded[0]?.content??''):null}:null;

    if (isOffscreen) {
        // Culled: positions come from the layout, so the frame stays and
        // the body is the count alone.
        return <div className="text-ink-3">{files.length} files</div>;
    }
    const shown = files.slice(0, files.length > FILES_SHOWN ? FILES_SHOWN - 1 : FILES_SHOWN);
    return (
        <>
            <CardMeta time={formatCardTime(cell.created_at)} by={author} />
            <CardSection icon={<File strokeWidth={1.6} />}>Files ({files.length})</CardSection>
            <ul className="m-0 mt-[0.1875rem] list-none whitespace-nowrap py-0 pl-[1.125rem] pr-0 leading-[1.6]">
                <FileRows paths={shown.map(f => f.name)} among={files.map(f => f.name)} />
                {files.length > shown.length && <li className="text-ink-3">+{files.length - shown.length} more</li>}
            </ul>
            {preview && (
                <>
                    <div className="mt-2.5 h-px bg-line" />
                    <div className="mt-[0.5625rem] flex min-w-0 items-center gap-1.5 whitespace-nowrap font-medium text-ink">
                        <File className="h-3 w-3 flex-none text-ink-3" strokeWidth={1.6} />
                        <span className="min-w-0 truncate">{splitName(preview.file.name).stem}</span>
                        <span className="flex-none font-mono text-[0.6875rem] font-normal text-ink-3">{splitName(preview.file.name).ext}</span>
                    </div>
                    {preview.text === null ? (
                        <p className="m-0 mt-1.5 text-ink-3">{error??'Loading preview…'}</p>
                    ) : (
                        <pre className="m-0 mt-1.5 max-h-24 overflow-hidden whitespace-pre-wrap break-words rounded-md border border-line bg-code-bg px-[0.5625rem] py-[0.4375rem] font-mono text-[0.65625rem] leading-normal text-ink-2">
                            {preview.text}
                        </pre>
                    )}
                </>
            )}
        </>
    );
});

import {useFileBodies} from '@/hooks/useFileBodies';
import {pinnedFileUrl} from '@/lib/fileBodies';
import type { Cell } from '@/lib/types';
import { currentChannelId } from '@/lib/currentChannel';
import { ScrollArea } from '@/components/ui/scroll-area';
import { MarkdownView } from '@/components/MarkdownView';
import { isImagePath } from '@/lib/fileKinds';

// Cheap, non-renderer stand-in for the pinned file: shown while the canvas is
// mid-zoom-transition, when mounting a real renderer would cost more than the
// glance it buys.
export function CompactPinnedPreview({ cell }: { cell: Cell }) {
    const pinnedFile = cell.files.find(f => f.name === cell.pinned);
    const {files:loaded,error}=useFileBodies(cell.channel || currentChannelId(),cell.id,pinnedFile?[pinnedFile]:[]);
    if (!pinnedFile) return null;
    const lower = pinnedFile.name.toLowerCase();
    const isMarkdown = lower.endsWith('.md') || lower.endsWith('.markdown') || lower.endsWith('.mdx');
    if(error)return <div role="alert">{error}</div>;
    const content=loaded?.[0]?.content;
    if(content===undefined)return <div>Loading preview…</div>;
    // This view lasts only for the zoom transition. Bound Markdown parsing;
    // the regular editor still receives the complete file after the transition.
    const markdownPreview = content.slice(0, 2048);
    if (isImagePath(pinnedFile.name)) {
        return (
            <div className="absolute inset-0 overflow-hidden flex items-center justify-center p-3">
                <img
                    src={pinnedFileUrl(cell.channel || currentChannelId(),cell.id,pinnedFile.name,pinnedFile.file_revision)}
                    alt={pinnedFile.name}
                    className="max-w-full max-h-full object-contain select-none"
                    draggable={false}
                />
            </div>
        );
    }
    return (
        <div className="absolute inset-0 overflow-hidden">
            <ScrollArea className="h-full">
                <div className="p-4 text-prose-ink">
                    {isMarkdown ? (
                        <>
                            <MarkdownView content={markdownPreview} className="text-sm" />
                            {content.length > markdownPreview.length && <span aria-label="Preview continues">…</span>}
                        </>
                    ) : (
                        <pre className="whitespace-pre-wrap break-words font-mono text-xs text-ink-2">{content}</pre>
                    )}
                </div>
            </ScrollArea>
        </div>
    );
}

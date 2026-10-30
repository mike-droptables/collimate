// The Files pane's history-mode surface: the selected run node's three
// collapsible sections — Config / Files / Logs.
//
// Config and Logs list the node's run artifacts (read-only, content served
// by hash); Files is the node's committed end state rendered through the
// ordinary FileTree with A/M/D badges vs the previous node. Nothing here
// ever publishes into filesPane.currentFolder/selection — those are the
// live import target and must never point at historical paths — and
// nothing here writes the live cell except the explicit "Copy to files"
// clone, which lands in the LIVE latest files as a normal editable file.

import React, { useMemo } from 'react';
import {
    ChevronDown, ChevronRight, Copy, GitBranch, Layers, LoaderCircle,
} from 'lucide-react';

import { HistoryArtifact, HistoryNode } from '@/lib/api';
import { buildFileTree, augmentTreeWithDeleted } from '@/lib/fileTree';
import { VirtualFile } from '@/lib/types';
import type { HistoryCategory } from '@/stores/collimate/types';
import { ScrollArea } from '@/components/ui/scroll-area';
import { FileTree } from '@/components/FileTreeNode';
import { getFileIcon } from '@/components/fileIcon';
import { cn } from '@/lib/utils';
import { iconBtn } from '@/components/ui/formStyles';

export type HistoryArtifactCategory = 'config' | 'log';

export interface HistorySectionsProps {
    node: HistoryNode | null;
    loading: boolean;
    error: string | null;
    /** The node's committed files (null while the tree loads). */
    files: VirtualFile[] | null;
    /** Previous node's contents — drives the A/M/D badges. Null while loading. */
    baseFiles: Record<string, string> | null;
    /** Nested file-version scrubbing state + toggle ('>' on the Files header,
     *  shown when the node has more than one version). */
    nested: boolean;
    onToggleNested: () => void;
    sectionsOpen: { config: boolean; files: boolean; logs: boolean };
    onToggleSection: (section: 'config' | 'files' | 'logs') => void;
    onOpenEntry: (category: HistoryCategory, path: string) => void;
    activeEntry: { category: HistoryCategory; path: string } | null;
    /** Clone a config/log artifact into the LIVE
     *  latest files. Absent = cloning unavailable (readOnly). */
    onCloneToFiles?: (category: HistoryArtifactCategory, path: string) => void;
    /** Branch a new cell from the node's selected version. Absent when the
     *  node has no versions. (History is left from the Files pane's Tree /
     *  Folder buttons; the panel has no exit control of its own.) */
    onBranch?: () => void;
}

const SECTION_LABEL: Record<'config' | 'files' | 'logs', string> = {
    config: 'Config', files: 'Files', logs: 'Logs',
};

const HISTORY_CLONE_MIME = 'application/x-collimate-history-clone';

function ArtifactRow({
    category, artifact, active, onOpen, onClone,
}: {
    category: HistoryArtifactCategory;
    artifact: HistoryArtifact;
    active: boolean;
    onOpen: () => void;
    onClone?: () => void;
}) {
    return (
        <div
            aria-current={active ? 'true' : undefined}
            className="group/history-file flex items-center gap-1.5 pl-4 pr-1 py-1 rounded-md cursor-pointer text-[0.8125rem] font-mono text-ink-2 hover:text-ink hover:bg-sheet-2 active:bg-sheet-3 transition-[background-color,color,scale] duration-150 motion-safe:active:scale-[0.99] motion-reduce:transition-none"
            onClick={onOpen}
            draggable={!!onClone}
            onDragStart={onClone ? (e) => {
                e.dataTransfer.setData(HISTORY_CLONE_MIME,
                    JSON.stringify({ category, path: artifact.path }));
                e.dataTransfer.setData('text/plain', artifact.path);
                e.dataTransfer.effectAllowed = 'copy';
            } : undefined}
            title={artifact.path}
        >
            <span className="flex-none">{getFileIcon('', artifact.path)}</span>
            <span className="truncate flex-1 min-w-0">{artifact.path}</span>
            {onClone && (
                <button
                    type="button"
                    title="Copy into the cell's files (editable clone)"
                    onClick={(e) => { e.stopPropagation(); onClone(); }}
                    className={cn(iconBtn, 'opacity-0 group-hover/history-file:opacity-100 p-0.5 hover:bg-sheet-3')}
                >
                    <Copy className="w-3 h-3" />
                </button>
            )}
        </div>
    );
}

export function HistorySections({
    node, loading, error, files, baseFiles,
    nested, onToggleNested,
    sectionsOpen, onToggleSection,
    onOpenEntry, activeEntry, onCloneToFiles, onBranch,
}: HistorySectionsProps) {
    const tree = useMemo(() => {
        if (!files) return null;
        const built = buildFileTree(files);
        return baseFiles
            ? augmentTreeWithDeleted(built, baseFiles, new Set(files.map(f => f.name)))
            : built;
    }, [files, baseFiles]);

    if (error) {
        return (
            <div className="flex-1 min-h-0 flex items-center justify-center p-3 text-[0.75rem] text-red text-center">
                History unavailable: {error}
            </div>
        );
    }
    if (loading || !node) {
        return (
            <div className="flex-1 min-h-0 flex items-center justify-center p-3 text-ink-3">
                <LoaderCircle className="w-4 h-4 animate-spin" />
            </div>
        );
    }

    const sectionHeader = (
        key: 'config' | 'files' | 'logs',
        count: number,
        extra?: React.ReactNode,
        onDouble?: () => void,
    ) => (
        <div className="w-full flex items-center">
            <button
                type="button"
                onClick={() => onToggleSection(key)}
                onDoubleClick={onDouble}
                title={onDouble ? 'Double-click to scrub this node\u2019s file versions' : undefined}
                className="flex-1 min-w-0 flex items-center gap-1.5 px-1.5 py-1.5 text-[0.6875rem] uppercase tracking-[0.08em] text-ink-3 hover:text-ink transition-colors select-none"
            >
                {sectionsOpen[key]
                    ? <ChevronDown className="w-3.5 h-3.5 flex-none" />
                    : <ChevronRight className="w-3.5 h-3.5 flex-none" />}
                <span className="flex-none font-semibold">{SECTION_LABEL[key]}</span>
                <span className="flex-none">{count}</span>
                <span className="flex-1" />
            </button>
            {extra}
        </div>
    );

    const emptyNote = (text: string) => (
        <div className="pl-6 py-1 text-[0.75rem] text-ink-3 italic">{text}</div>
    );

    const runLabel = node.kind === 'run'
        ? `${node.run?.runner_id ?? node.run?.generator_name ?? 'run'}`
        : 'File edits';
    const runStatus = node.kind === 'run' ? (node.run?.status ?? '') : '';
    // runs.status also holds a live run's 'running', which runStop.isFailureStatus (terminal statuses only) would misread.
    const runFailed = runStatus === 'failed' || runStatus === 'timed_out' || runStatus === 'interrupted';

    return (
        <ScrollArea fitWidth type="scroll" className="flex-1 min-h-0">
            <div
                className="p-1.5 pb-2"
                // The Files section is the drop target for a dragged
                // Config/Logs row — dropping clones the artifact into the
                // LIVE latest files (same action as the row's Copy button).
                onDragOver={onCloneToFiles ? (e) => {
                    if (e.dataTransfer.types.includes(HISTORY_CLONE_MIME)) {
                        e.preventDefault();
                        e.dataTransfer.dropEffect = 'copy';
                    }
                } : undefined}
                onDrop={onCloneToFiles ? (e) => {
                    const raw = e.dataTransfer.getData(HISTORY_CLONE_MIME);
                    if (!raw) return;
                    e.preventDefault();
                    try {
                        const { category, path } = JSON.parse(raw);
                        if ((category === 'config' || category === 'log') && typeof path === 'string') {
                            onCloneToFiles(category, path);
                        }
                    } catch { /* malformed payload — ignore */ }
                } : undefined}
            >
                {/* Node header — double-click drills into the node's file
                    versions when it has more than one. */}
                <div
                    className="px-1.5 pb-2 border-b border-line mb-1"
                    onDoubleClick={node.versions.length > 1 ? onToggleNested : undefined}
                    title={node.versions.length > 1
                        ? 'Double-click to scrub this node\u2019s file versions'
                        : undefined}
                >
                    <div className="flex items-center gap-2 min-w-0">
                        {/* The run light's grammar (the mock's `.rs`): a
                            ring at rest, teal while running, green for a
                            pass, a red ring for a failure, a small square
                            when stopped. */}
                        <span data-testid="history-node-light" className={cn(
                            'w-2.5 h-2.5 rounded-full border-[1.5px] border-ink-3 flex-none',
                            runStatus === 'running' && 'border-teal bg-teal animate-pulse',
                            runFailed && 'border-red',
                            runStatus === 'completed' && 'border-green bg-green',
                            runStatus === 'stopped' && 'w-2 h-2 border-0 rounded-[1.5px] bg-ink-3',
                        )} />
                        <span className="text-[0.8125rem] font-mono text-ink truncate flex-1 min-w-0" title={runLabel}>
                            {runLabel}
                        </span>
                        {onBranch && (
                            <button
                                type="button"
                                title="Branch a new cell from this version"
                                onClick={onBranch}
                                className={cn(iconBtn, 'p-0.5')}
                            >
                                <GitBranch className="w-3.5 h-3.5" />
                            </button>
                        )}
                    </div>
                    {node.kind === 'run' && node.run?.prompt ? (
                        <div className="text-[0.75rem] text-ink-2 truncate mt-1" title={node.run.prompt}>
                            {node.run.prompt}
                        </div>
                    ) : null}
                    <div className="flex items-center gap-2 mt-1">
                        {node.kind === 'run' && (
                            <span className="text-[0.6875rem] text-ink-3">
                                {runStatus || 'pending'}
                                {node.logs.length === 0 && runStatus === 'running' ? ' · log pending' : ''}
                            </span>
                        )}
                        {node.versions.length > 1 && (
                            <button
                                type="button"
                                onClick={onToggleNested}
                                title={nested
                                    ? 'Back to run-node scrubbing'
                                    : `Scrub this node's ${node.versions.length} file versions (double-click the node also works)`}
                                className={cn(
                                    'flex items-center gap-1 px-1.5 h-5 rounded-xs border text-[0.6875rem] font-mono flex-none transition-colors',
                                    nested
                                        ? 'text-teal border-teal bg-teal-soft'
                                        : 'text-ink-2 border-line-2 hover:text-ink hover:bg-sheet-2',
                                )}
                            >
                                <Layers className="w-3 h-3" />
                                {node.versions.length} versions
                            </button>
                        )}
                    </div>
                </div>

                {/* Config */}
                {sectionHeader('config', node.config.length)}
                {sectionsOpen.config && (
                    node.config.length === 0
                        ? emptyNote(node.kind === 'run' ? 'No config captured' : 'No run — no config')
                        : node.config.map(a => (
                            <ArtifactRow
                                key={`config:${a.path}`}
                                category="config"
                                artifact={a}
                                active={activeEntry?.category === 'config' && activeEntry.path === a.path}
                                onOpen={() => onOpenEntry('config', a.path)}
                                onClone={onCloneToFiles ? () => onCloneToFiles('config', a.path) : undefined}
                            />
                        ))
                )}

                {/* Files — double-click the header to drill into the
                    node's file versions. */}
                {sectionHeader('files', files?.length ?? node.versions[node.versions.length - 1]?.file_count ?? 0,
                    node.versions.length > 1 ? (
                        <button
                            type="button"
                            title={nested
                                ? 'Back to run-node scrubbing'
                                : `Scrub this node's ${node.versions.length} file versions`}
                            onClick={onToggleNested}
                            className={cn(
                                'flex items-center gap-1 flex-none px-1 rounded-xs font-mono text-[0.6875rem] normal-case',
                                nested ? 'text-teal bg-teal-soft' : 'text-ink-3 hover:text-ink hover:bg-sheet-2',
                            )}
                        >
                            <Layers className="w-3 h-3" />
                            {node.versions.length}
                        </button>
                    ) : undefined,
                    node.versions.length > 1 ? onToggleNested : undefined)}
                {sectionsOpen.files && (
                    files === null ? emptyNote('Loading…')
                        : files.length === 0 ? emptyNote(node.versions.length === 0 ? 'No versions from this run' : 'No files')
                            : tree && (
                                // The rows' @container: a narrow pane drops
                                // their file / folder icons.
                                <div className="@container">
                                    <FileTree
                                        nodes={tree}
                                        level={0}
                                        onOpenFile={(path) => onOpenEntry('file', path)}
                                        activePath={activeEntry?.category === 'file' ? activeEntry.path : null}
                                        onSelectionChange={() => { /* never publish — the import target stays live */ }}
                                        onCreateFile={() => { }}
                                        onRename={() => { }}
                                        onDelete={() => { }}
                                        readOnly
                                        onDropFiles={() => { }}
                                        diffTargetFiles={baseFiles ?? undefined}
                                    />
                                </div>
                            )
                )}

                {/* Logs */}
                {sectionHeader('logs', node.logs.length)}
                {sectionsOpen.logs && (
                    node.logs.length === 0
                        ? emptyNote(node.kind === 'run'
                            ? (runStatus === 'running' ? 'Log pending — run in flight' : 'No log captured')
                            : 'No run — no log')
                        : node.logs.map(a => (
                            <ArtifactRow
                                key={`log:${a.path}`}
                                category="log"
                                artifact={a}
                                active={activeEntry?.category === 'log' && activeEntry.path === a.path}
                                onOpen={() => onOpenEntry('log', a.path)}
                                onClone={onCloneToFiles ? () => onCloneToFiles('log', a.path) : undefined}
                            />
                        ))
                )}
            </div>
        </ScrollArea>
    );
}

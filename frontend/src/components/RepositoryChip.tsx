import { useRef, useState } from 'react';
import { Download, GitBranch, RotateCcw, Trash2, TriangleAlert } from 'lucide-react';
import type { CellRepository } from '@/lib/types';
import { Menu } from '@/components/ui/Menu';
import { menuItem, menuItemDanger, menuSep } from '@/components/ui/menuStyles';
import { chip } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { branchLabel, keptOnTip, repositoryPlace, repositoryTitle, rescueText } from '@/lib/repositories';
import { discardRescue, rescueExportUrl, retryRescue } from '@/lib/api/repositories';
import { offerDuplicate, refusalText, repositoryBusyMessage } from '@/lib/repositoryBusy';
import { exportFileName, saveCheckedZip } from '@/lib/transfer/exportSinks';
import { useCollimateStore } from '@/stores/useCollimateStore';

export interface RepositoryChipProps {
    repo: CellRepository;
    /** The cell and its channel, for a rescue's actions. */
    cellId: string;
    channel: string;
    /** A view that changes nothing (a diff, version history): the chip
     *  informs only. */
    readOnly?: boolean;
    /** The host's width rule over the chip's own (a folder tile's). */
    className?: string;
}

/** What Discard's confirmation says: the rescue goes, and with a repository
 *  on the tip at its path that repository's history leaves the cell too. */
function discardBody(repo: CellRepository): string {
    const rescue = 'The git history a run could not save is deleted.';
    if (!keptOnTip(repo)) return `${rescue} It cannot be recovered.`;
    const which = repo.path ? `the repository at ${repo.path}` : 'the cell root’s repository';
    return `${rescue} The history of ${which} (its branches, stash and reflogs) is removed from the cell too; its files stay.`;
}

/**
 * A repository's branch chip (D5) on the cell head (the root's) or its
 * folder's row: `main`, `main ↑2 ↓1` against its upstream, or `(detached)`,
 * the hover naming the upstream or what git refused. At most about 10rem;
 * the branch truncates first. A rescue (a run's history the harvest could
 * not keep) turns it into a button whose menu says "history not saved:
 * <reason>" and offers Retry, Export and Discard.
 */
export function RepositoryChip({ repo, cellId, channel, readOnly = false, className }: RepositoryChipProps) {
    const [open, setOpen] = useState(false);
    const [busy, setBusy] = useState(false);
    const anchor = useRef<HTMLButtonElement>(null);
    const rescue = repo.error?.rescued ? repo.error : null;
    const title = repositoryTitle(repo);
    const look = cn(chip, 'flex-none max-w-[10rem] font-mono', repo.error && 'border-red/40 text-red', className);
    const body = <>
        <GitBranch aria-hidden className="h-3 w-3 flex-none" />
        <span className="min-w-0 truncate">{branchLabel(repo)}</span>
        {!!repo.ahead && <span data-testid="repo-chip-ahead" className="flex-none tabular-nums">↑{repo.ahead}</span>}
        {!!repo.behind && <span data-testid="repo-chip-behind" className="flex-none tabular-nums">↓{repo.behind}</span>}
        {repo.error && <span data-testid="repo-chip-error" className="flex-none inline-flex" aria-label={rescue ? rescueText(rescue.reason) : repo.error.reason}>
            <TriangleAlert aria-hidden className="h-3 w-3" />
        </span>}
    </>;
    if (!rescue || readOnly) return <span data-testid="repo-chip" className={look} title={title}>{body}</span>;

    /** One rescue action: a lease refusal offers Duplicate, anything else
     *  says what the host said. */
    const act = async (failTitle: string, work: () => Promise<unknown>) => {
        setOpen(false);
        setBusy(true);
        try {
            await work();
        } catch (error) {
            const held = repositoryBusyMessage(error);
            if (held) await offerDuplicate(cellId, held);
            else await useCollimateStore.getState().openAlert({ cellId, title: failTitle, body: refusalText(error) });
        } finally {
            setBusy(false);
        }
    };
    const discard = async () => {
        setOpen(false);
        const ok = await useCollimateStore.getState().openConfirm({
            cellId, title: 'Discard git history', body: discardBody(repo), confirmLabel: 'Discard', destructive: true,
        });
        if (ok) await act('Discard failed', () => discardRescue(channel, cellId, repo.path));
    };

    return (
        // A click or key in the menu (a portal) bubbles through the React
        // tree: it must neither select the card nor toggle, enter or delete
        // the folder row or tile the chip sits on.
        <span className={cn('flex flex-none min-w-0', className)} onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
            <button
                ref={anchor}
                type="button"
                data-testid="repo-chip"
                className={cn(look, 'cursor-pointer hover:bg-red-soft')}
                title={title}
                aria-haspopup="dialog"
                aria-expanded={open}
                disabled={busy}
                onPointerDown={(e) => { if (e.button === 0) e.stopPropagation(); }}
                onClick={() => setOpen(o => !o)}
            >
                {body}
            </button>
            <Menu anchor={anchor} open={open} onClose={() => setOpen(false)} role="dialog" label={`Git history ${repositoryPlace(repo.path)}`} align="start" minWidth={248} maxWidth={320}>
                <div className="flex flex-col">
                    <p data-testid="repo-rescue-reason" className="px-2.5 py-1.5 text-[0.75rem] leading-snug text-ink-2 break-words">{rescueText(rescue.reason)}</p>
                    <div className={menuSep} />
                    <button type="button" className={menuItem} title="Keep this history in the cell, as the run would have"
                        onClick={() => void act('Retry failed', () => retryRescue(channel, cellId, repo.path))}>
                        <RotateCcw className="h-3.5 w-3.5" />Retry
                    </button>
                    <button type="button" className={menuItem} title="Download the rescued .git as a zip"
                        onClick={() => void act('Export failed', () => saveCheckedZip(rescueExportUrl(channel, cellId, repo.path),
                            `${exportFileName(repo.path.split('/').pop() || 'cell', 'cell')} git history.zip`))}>
                        <Download className="h-3.5 w-3.5" />Export
                    </button>
                    <button type="button" className={menuItemDanger} onClick={() => void discard()}>
                        <Trash2 className="h-3.5 w-3.5" />Discard…
                    </button>
                </div>
            </Menu>
        </span>
    );
}

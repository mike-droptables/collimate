import { useEffect, useRef, useSyncExternalStore } from 'react';
import { createPortal } from 'react-dom';
import { motion, AnimatePresence } from 'motion/react';
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { btn, btnDanger, btnPrimary, dialog, iconBtn } from '@/components/ui/formStyles';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';

/** The cells whose editors host their own confirms right now. A dialog
 *  about a cell shows in that cell's host when one is mounted, else in the
 *  app's instance, so a question never goes unseen. */
const hosts = new Set<string>();
const hostListeners = new Set<() => void>();
let hostsVersion = 0;
const subscribeHosts = (listener: () => void) => { hostListeners.add(listener); return () => { hostListeners.delete(listener); }; };
const useHostsVersion = () => useSyncExternalStore(subscribeHosts, () => hostsVersion);
function setHosted(cellId: string, hosted: boolean) {
    if (hosted) hosts.add(cellId); else hosts.delete(cellId);
    hostsVersion++;
    hostListeners.forEach(listener => listener());
}

/**
 * Confirm / alert / choice modal. Reads its state from the store's
 * `confirmDialog` slice (populated by `openConfirm` / `openAlert` /
 * `openChoice`).
 *
 * The app's instance (mounted once at the root, no `cellId`) shows every
 * dialog about nothing in particular, portalled to the body above the
 * whole window. A cell's instance (`cellId`, mounted by its editor in the
 * pane region) shows the dialogs about that cell's files, as a modal of
 * the cell: a scrim over its body and the sheet centred in it, concealing
 * only that cell's guests. The app's instance stands in for a cell whose
 * editor is not mounted.
 *
 * Visual shell intentionally matches `ChannelDialog` in AppTopBar — same
 * motion entrance, same header/body/footer structure — so every prompt
 * in the app feels consistent instead of bouncing to a native browser
 * dialog.
 */
export function ConfirmDialog({ cellId }: { cellId?: string } = {}) {
    const dialog = useCollimateStore(s => s.confirmDialog);
    const resolveConfirm = useCollimateStore(s => s.resolveConfirm);
    const resolveChoice = useCollimateStore(s => s.resolveChoice);
    useHostsVersion();
    useEffect(() => {
        if (!cellId) return;
        setHosted(cellId, true);
        return () => setHosted(cellId, false);
    }, [cellId]);
    const shown = dialog && (cellId ? dialog.cellId === cellId : !(dialog.cellId && hosts.has(dialog.cellId)));

    return (
        <AnimatePresence>
            {shown && (
                <ConfirmDialogInner
                    key="confirm-dialog"
                    cellId={cellId}
                    kind={dialog.kind}
                    title={dialog.title}
                    body={dialog.body}
                    confirmLabel={dialog.kind === 'choice' ? 'OK' : dialog.confirmLabel}
                    cancelLabel={dialog.kind === 'choice' ? '' : dialog.cancelLabel}
                    destructive={dialog.kind === 'choice' ? false : dialog.destructive}
                    options={dialog.kind === 'choice' ? dialog.options : undefined}
                    onResolve={(result) => {
                        if (dialog.kind === 'choice') resolveChoice(result as string | null);
                        else resolveConfirm(result as boolean);
                    }}
                />
            )}
        </AnimatePresence>
    );
}

interface ChoiceOption { id: string; label: string; destructive?: boolean }

interface InnerProps {
    /** The cell this is a modal of; absent, the whole app's. */
    cellId?: string;
    kind: 'confirm' | 'alert' | 'choice';
    title: string;
    body: string;
    confirmLabel: string;
    cancelLabel: string;
    destructive: boolean;
    options?: ChoiceOption[];
    onResolve: (result: boolean | string | null) => void;
}

function ConfirmDialogInner({
    cellId, kind, title, body, confirmLabel, cancelLabel, destructive, options, onResolve,
}: InnerProps) {
    const rootRef = useRef<HTMLDivElement>(null);
    // Mounted means open (the outer switch gates on `dialog`); a native
    // guest view would paint over the dialog otherwise: the cell's own
    // guests for a cell's dialog, every guest for the app's.
    useGuestOccluder(cellId ? { cellIds: [cellId] } : 'all');
    const isChoice = kind === 'choice';

    // Cancel resolves false (confirm/alert) or null (choice).
    const cancel = () => onResolve(isChoice ? null : false);
    // Primary resolves true (confirm/alert). Choice has no implicit
    // primary — Enter is a no-op there.
    const confirm = () => { if (!isChoice) onResolve(true); };

    useEffect(() => {
        rootRef.current?.querySelector<HTMLButtonElement>('[data-confirm-primary]')?.focus();
    }, []);

    useEffect(() => {
        const handleKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') cancel();
            // A focused button's native click owns Enter (Cancel / X must not confirm).
            if (e.key === 'Enter' && (e.target as Element | null)?.closest?.('button')) return;
            if (e.key === 'Enter') confirm();
        };
        document.addEventListener('keydown', handleKey);
        return () => document.removeEventListener('keydown', handleKey);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [isChoice]);

    // App-modal: above the full-window view (z-[450]), the Commands pane
    // over it (z-[455]) and the menus and dialogs those open (z-[460]), so
    // a confirm asked from any of them shows; below the shutdown overlay.
    // Cell-modal: over the cell's pane region, above its panel (z 5).
    const sheet = (
        <div
            data-confirm-host={cellId ?? 'app'}
            className={cn('flex items-center justify-center p-4 bg-(--lift) backdrop-blur-[0.25rem]',
                cellId ? 'absolute inset-0 z-[6]' : 'fixed inset-0 z-[480]')}
            onClick={(e) => { if (e.target === e.currentTarget) cancel(); }}
        >
            <motion.div
                ref={rootRef}
                initial={{ opacity: 0, scale: 0.96, y: 8 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.96, y: 8 }}
                transition={{ duration: 0.12 }}
                className={cn(dialog, 'w-full max-w-sm')}
                role="dialog"
                aria-modal="true"
                {...(cellId ? KEYBOARD_OWNER_PROPS : {})}
            >
                <div className="flex items-center justify-between h-9 px-4 border-b border-line">
                    <h2 className="text-[0.84375rem] font-semibold text-ink">{title}</h2>
                    <button onClick={cancel} className={cn(iconBtn, 'h-[1.375rem] w-[1.375rem]')} aria-label="Close">
                        <X className="w-4 h-4" />
                    </button>
                </div>
                <div className="p-4 space-y-3">
                    {body && <p className="text-[0.78125rem] text-ink-2 leading-relaxed whitespace-pre-wrap">{body}</p>}
                </div>
                <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-line">
                    {isChoice ? (
                        <>
                            <button className={btn} onClick={cancel}>Cancel</button>
                            {(options ?? []).map((opt, i) => (
                                <button
                                    key={opt.id}
                                    {...(i === 0 ? { 'data-confirm-primary': true } : {})}
                                    className={opt.destructive ? btnDanger : btnPrimary}
                                    onClick={() => onResolve(opt.id)}
                                >
                                    {opt.label}
                                </button>
                            ))}
                        </>
                    ) : (
                        <>
                            {kind === 'confirm' && cancelLabel && (
                                <button className={btn} onClick={cancel}>
                                    {cancelLabel}
                                </button>
                            )}
                            <button
                                data-confirm-primary
                                className={destructive ? btnDanger : btnPrimary}
                                onClick={confirm}
                            >
                                {confirmLabel}
                            </button>
                        </>
                    )}
                </div>
            </motion.div>
        </div>
    );
    return cellId ? sheet : createPortal(sheet, document.body);
}

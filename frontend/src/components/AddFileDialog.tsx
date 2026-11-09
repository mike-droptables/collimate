import { useViewportClampedPosition, useDismissOnOutside } from '@/hooks/usePopoverPosition';
import React, { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { btn, btnPrimary, dialog, formInput, sectionLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { useRendererId, useSupportedExtensions } from '@/hooks/useRendererId';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';
import { filenameCompletions } from '@/lib/filenameCompletions';

/** Fixed-position popover for creating a file or folder in a given location.
 *  Files collect a full filename, with optional inline extension completion.
 *  The folder mode collects a single segment name. */
interface AddFileDialogProps {
    x: number;
    y: number;
    /** Resolved current location ('' = cell root). The file/folder lands here. */
    basePath: string;
    /** Full paths already in the cell, for duplicate detection. */
    existingNames: string[];
    mode: 'file' | 'folder';
    /** The header's verb ("Paste image"); defaults to New file / New folder. */
    title?: string;
    /** The initial name, combined with initialExt; only the stem is selected. */
    initialStem?: string;
    /** The initial extension in file mode, including custom or empty ones. */
    initialExt?: string;
    onCancel: () => void;
    /** Bare filename incl. extension (file mode) or a single folder segment.
     *  A `{ ok: false }` result keeps the dialog open showing its error; the
     *  caller unmounts the dialog on success. */
    onCreate: (name: string) => { ok: true } | { ok: false; error: string } | void | Promise<{ok:true}|{ok:false;error:string}|void>;
}

export function AddFileDialog({ x, y, basePath, existingNames, mode, title, initialStem = '', initialExt, onCancel, onCreate }: AddFileDialogProps) {
    // Mounted means open (the FileBrowser/FolderView callers gate it); the
    // popover is a body portal a native guest view would paint over.
    useGuestOccluder('all');
    const exts = useSupportedExtensions();
    const [filename, setFilename] = useState(() => initialStem + (mode === 'file' && initialExt
        ? (initialExt.startsWith('.') ? initialExt : '.' + initialExt)
        : ''));
    // Cycling changes only the completion, never the query used to filter it.
    const [completionIndex, setCompletionIndex] = useState(0);
    const [completionAccepted, setCompletionAccepted] = useState(false);
    const [focused, setFocused] = useState(false);
    const [caretAtEnd, setCaretAtEnd] = useState(true);
    const [scrollLeft, setScrollLeft] = useState(0);
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const inputRef = useRef<HTMLInputElement>(null);
    const panelRef = useRef<HTMLDivElement>(null);
    const hintId = useId();

    const pos = useViewportClampedPosition(panelRef, x, y);
    useDismissOnOutside(panelRef, () => { if (!busy) onCancel(); });

    const completions = mode === 'file' && focused && caretAtEnd && !busy && !completionAccepted
        ? filenameCompletions(filename, exts)
        : [];
    const completion = completions.length ? completions[completionIndex % completions.length] : undefined;
    const suffix = completion?.slice(filename.length) ?? '';
    // The hint names the renderer of the name as typed: what Enter creates
    // now, never what a completion would make of it once accepted.
    const { renderer, error: rendererError } = useRendererId(filename.trim());

    useEffect(() => {
        inputRef.current?.focus();
        inputRef.current?.setSelectionRange(0, initialStem.length);
        // Initial props seed this prompt; subsequent edits belong to the user.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const submit = async () => {
        if (busy) return;
        const name = filename.trim();
        if (name.length === 0) { setError(mode === 'folder' ? 'Enter a folder name' : 'Enter a file name'); return; }
        if (name.includes('/')) { setError("Name can't contain /"); return; }
        const full = basePath ? `${basePath}/${name}` : name;
        if (existingNames.includes(full)) { setError(`\`${full}\` already exists`); return; }
        setBusy(true);
        setError(null);
        try {
            const res = await onCreate(name);
            if (res && !res.ok) setError(res.error);
        } catch (error) {
            setError(String((error as Error)?.message ?? error));
        } finally {
            setBusy(false);
        }
    };

    const heading = title ?? (mode === 'folder' ? 'New folder' : 'New file');

    return createPortal(
        <div
            ref={panelRef}
            role="dialog"
            aria-label={heading}
            className={cn('fixed z-[460] w-[20rem] p-2.5 flex flex-col gap-2 animate-in fade-in zoom-in-95 duration-100', dialog)}
            // no-drag: keeps the popup interactive when it opens inside the
            // desktop title-bar drag band (see FullscreenContentOverlay).
            style={{ WebkitAppRegion: 'no-drag', left: pos.x, top: pos.y } as React.CSSProperties}
            onClick={(e) => e.stopPropagation()}
            // Its Enter and Esc are its own: the scene's capture-phase
            // listener yields to it.
            {...KEYBOARD_OWNER_PROPS}
        >
            <div className={sectionLabel}>
                {heading} in{' '}
                <span className="font-mono font-normal text-ink normal-case tracking-normal">{basePath || 'root'}</span>
            </div>
            <div className="relative">
                <input
                    ref={inputRef}
                    value={filename}
                    disabled={busy}
                    aria-label={mode === 'folder' ? 'Folder name' : 'File name'}
                    aria-autocomplete={mode === 'file' ? 'inline' : undefined}
                    aria-describedby={mode === 'file' ? hintId : undefined}
                    autoComplete="off"
                    spellCheck={false}
                    onFocus={() => setFocused(true)}
                    onBlur={() => setFocused(false)}
                    onSelect={(e) => {
                        const input = e.currentTarget;
                        setCaretAtEnd(input.selectionStart === input.value.length && input.selectionEnd === input.value.length);
                    }}
                    onScroll={(e) => setScrollLeft(e.currentTarget.scrollLeft)}
                    onChange={(e) => {
                        setFilename(e.target.value);
                        setCompletionIndex(0);
                        setCompletionAccepted(false);
                        setCaretAtEnd(e.target.selectionStart === e.target.value.length && e.target.selectionEnd === e.target.value.length);
                        setError(null);
                    }}
                    onKeyDown={(e) => {
                        if (!e.nativeEvent.isComposing) {
                            if (completion && e.key === 'Tab' && !e.shiftKey) {
                                e.preventDefault();
                                setFilename(completion);
                                setCompletionIndex(0);
                                setCompletionAccepted(true);
                                setError(null);
                            } else if (completion && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
                                e.preventDefault();
                                setCompletionIndex(index => (index + (e.key === 'ArrowDown' ? 1 : completions.length - 1)) % completions.length);
                            } else if (e.key === 'Enter') {
                                e.preventDefault();
                                submit();
                            } else if (e.key === 'Escape' && !busy) {
                                e.preventDefault();
                                onCancel();
                            }
                        }
                        e.stopPropagation();
                    }}
                    placeholder={mode === 'folder' ? 'folder-name' : 'filename'}
                    className={cn(formInput, 'font-mono', error && 'border-red focus:border-red')}
                />
                {completion && (
                    <div aria-hidden="true" data-filename-completion className="pointer-events-none absolute inset-y-0 inset-x-px flex items-center overflow-hidden px-2.5 font-mono text-[0.8125rem]">
                        <span className="whitespace-pre" style={{ transform: `translateX(${-scrollLeft}px)` }}>
                            <span className="invisible">{filename}</span><span className="text-ink-3">{suffix}</span>
                        </span>
                    </div>
                )}
            </div>
            {mode === 'file' && (
                <div id={hintId} className="text-[0.6875rem] text-ink-3 [overflow-wrap:anywhere]" aria-live="polite">
                    <div>
                        Default renderer: <span className="text-ink">{renderer?.name ?? (rendererError ? 'Unavailable' : 'Loading…')}</span>
                    </div>
                    {completion && <div>Tab to accept · ↑↓ to change extension</div>}
                </div>
            )}
            {error && <div className="text-[0.6875rem] text-red">{error}</div>}
            <div className="flex justify-end gap-1.5">
                <button type="button" onClick={onCancel} disabled={busy} className={btn}>Cancel</button>
                <button type="button" onClick={submit} disabled={busy} className={btnPrimary}>
                    {busy ? 'Creating…' : 'Create'}
                </button>
            </div>
        </div>,
        document.body,
    );
}

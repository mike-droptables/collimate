import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { carriesOsFiles } from '@/lib/fileDrag';

export interface InCellDropFrameState {
    /** Where the OS file drag over the surface would land: a folder path
     *  ('' = the cell root) while the frame is lit, null while it is dark. */
    target: string | null;
    /** Called from the surface's dragover: the folder the drag would land in
     *  when the surface takes it, null when it refuses it (a `_runs` folder).
     *  Ignores every drag but an OS file drag. */
    aim: (e: React.DragEvent, folder: string | null) => void;
    /** Spread on the surface's root. */
    frameProps: {
        onDragEnterCapture: (e: React.DragEvent) => void;
        onDragLeaveCapture: (e: React.DragEvent) => void;
        onDropCapture: () => void;
    };
}

/** The lit state of a Files pane's in-cell drop target (InCellDropFrame):
 *  an OS file drag over a writable tree or Folder view shows where the
 *  files will land, inside this cell, as opposed to the canvas's NEW CELL
 *  slot.
 *
 *  The surface's dragover aims it on the branches that take the drop and
 *  darkens it on the ones that refuse it; a read-only surface never aims.
 *  The root's CAPTURE-phase dragenter/dragleave count the elements the drag
 *  is inside, and the frame goes dark when the count unwinds: capture,
 *  because a tree's folder row stops its own dragleave; a count, because
 *  Chromium gives an OS file drag's dragleave a null relatedTarget at child
 *  boundaries. A drop fires no dragleave, so the root's drop and any
 *  window-level drop or dragend (capture: no stopPropagation hides them)
 *  reset it too. */
export function useInCellDropFrame(): InCellDropFrameState {
    const depth = useRef(0);
    const [target, setTarget] = useState<string | null>(null);

    useEffect(() => {
        const reset = () => { depth.current = 0; setTarget(null); };
        window.addEventListener('drop', reset, true);
        window.addEventListener('dragend', reset, true);
        return () => {
            window.removeEventListener('drop', reset, true);
            window.removeEventListener('dragend', reset, true);
        };
    }, []);

    const aim = useCallback((e: React.DragEvent, folder: string | null) => {
        if (carriesOsFiles(e.dataTransfer)) setTarget(folder);
    }, []);

    const frameProps = useMemo(() => ({
        onDragEnterCapture: (e: React.DragEvent) => {
            if (carriesOsFiles(e.dataTransfer)) depth.current += 1;
        },
        onDragLeaveCapture: (e: React.DragEvent) => {
            if (!carriesOsFiles(e.dataTransfer)) return;
            depth.current = Math.max(0, depth.current - 1);
            if (depth.current === 0) setTarget(null);
        },
        onDropCapture: () => { depth.current = 0; setTarget(null); },
    }), []);

    return { target, aim, frameProps };
}

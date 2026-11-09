import { useEffect } from 'react';
import { motion } from 'motion/react';
import { Power } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useGuestOccluder } from '@/lib/guestOcclusion';

const CLOSE_AFTER_MS = 6000;

/**
 * Full-screen overlay shown between receiving a `shutdown` SSE event and
 * the window actually going away. A full shutdown is the desktop shell's
 * quit sequence (POST /api/system/shutdown, then signals), so this is
 * what the Electron window shows while the server stops every generator
 * and sandbox; the scheduled `window.close()` closes it cleanly once
 * teardown has had its window, rather than leaving it pointed at an
 * origin that is already gone.
 *
 * In a plain browser tab close() silently fails (the tab wasn't
 * script-opened) — we then drop the isShuttingDown flag so the existing
 * ServerDownScreen takes over, rather than stranding the user on this
 * overlay forever.
 */
export function ShutdownOverlay() {
    const setShuttingDown = useCollimateStore(s => s.setShuttingDown);
    const fetchState = useCollimateStore(s => s.fetchState);

    // StackApp replaces the whole tree with this overlay, so the canvas's
    // guests unmount with it — but native views outlive React unmounts by
    // a beat, and nothing may paint over the shutdown screen.
    useGuestOccluder('all');

    useEffect(() => {
        const t = window.setTimeout(() => {
            window.close();
            // In the Electron window this line never runs — the window
            // is already gone. In a regular tab, close() is a
            // no-op — drop the flag and probe the server so apiError
            // flips immediately and ServerDownScreen takes over without
            // a frame of stale UI.
            setShuttingDown(false);
            void fetchState();
        }, CLOSE_AFTER_MS);
        return () => window.clearTimeout(t);
    }, [setShuttingDown, fetchState]);

    return (
        <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.15 }}
            className="fixed inset-0 z-[500] flex flex-col items-center justify-center gap-3 bg-canvas text-ink"
        >
            <Power className="w-6 h-6 text-ink-3 animate-pulse" />
            <div className="text-[0.84375rem] text-ink-2 tracking-wide">
                Shutting down…
            </div>
        </motion.div>
    );
}

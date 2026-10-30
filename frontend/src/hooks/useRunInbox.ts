import { useCallback, useEffect, useRef, useState } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { fetchRunInbox, type RunInboxPointer } from '@/lib/api/annotations';
import {
    INBOX_CLOSE_GONE, INBOX_CLOSE_UNAUTHORIZED, INBOX_RETRY_LIMIT, INBOX_RETRY_MS,
    answerFrame, applyInboxFrame, inboxRedialMs, inboxSocketUrl, type PendingPermission,
} from '@/lib/runInbox';

export type RunInboxStatus = 'off' | 'pending' | 'connecting' | 'open' | 'ended';

export interface RunInbox {
    /** The run's open approval cards. */
    cards: PendingPermission[];
    status: RunInboxStatus;
    /** Answer a card (an option id, or null to cancel it); false while the
     *  socket is not open. */
    answer: (id: string, optionId: string | null) => boolean;
}

const NO_CARDS: PendingPermission[] = [];

/**
 * A live run's approval cards (a drilldown's ApprovalCards): the run's
 * broadcast inbox (GET /runs/{id}/inbox, then its WebSocket), which several
 * viewers may attach to at once. Until the run registers its inbox the
 * pointer is asked for again every INBOX_RETRY_MS (at once when the run's
 * phase changes), INBOX_RETRY_LIMIT times. A socket that closes as gone or
 * unauthorized fetches the pointer once more (a new one dials, none ends
 * it); any other close redials after a growing wait. Once ended, each phase
 * change of the run asks for the pointer once more. Only the frames that
 * carry cards change them (lib/runInbox applyInboxFrame).
 */
export function useRunInbox(channel: string, runId: string, live: boolean): RunInbox {
    const [cards, setCards] = useState<PendingPermission[]>(NO_CARDS);
    const [status, setStatus] = useState<RunInboxStatus>(live ? 'pending' : 'off');
    // Another run, channel or liveness starts afresh (reset during render,
    // before the effect below dials).
    const key = `${channel}\0${runId}\0${live}`;
    const [seen, setSeen] = useState(key);
    if (seen !== key) {
        setSeen(key);
        setCards(NO_CARDS);
        setStatus(live ? 'pending' : 'off');
    }
    const socketRef = useRef<WebSocket | null>(null);
    const kickRef = useRef<(() => void) | null>(null);
    const phase = useCollimateStore(s => s.runPhases[runId]?.phase);

    useEffect(() => {
        if (!live) return;
        let cancelled = false;
        let timer: ReturnType<typeof setTimeout> | null = null;
        let tries = 0;
        let attempt = 0;
        const clear = () => {
            if (timer !== null) clearTimeout(timer);
            timer = null;
        };
        // Ended, a phase change still asks once more: a run may register its
        // inbox late (the cards it raises come with an awaiting_approval).
        const end = () => {
            setStatus('ended');
            setCards(NO_CARDS);
            kickRef.current = () => void lookup(false);
        };

        const dial = (pointer: RunInboxPointer, refetched: boolean) => {
            setStatus('connecting');
            const socket = new WebSocket(inboxSocketUrl(pointer.ws_path, pointer.token));
            socketRef.current = socket;
            socket.onopen = () => {
                if (!cancelled) setStatus('open');
            };
            socket.onmessage = (event: MessageEvent) => {
                if (cancelled) return;
                attempt = 0;
                let frame: unknown;
                try {
                    frame = JSON.parse(String(event.data));
                } catch {
                    return;
                }
                setCards(c => applyInboxFrame(c, frame));
            };
            socket.onclose = (event: CloseEvent) => {
                if (cancelled || socketRef.current !== socket) return;
                socketRef.current = null;
                if (event.code === INBOX_CLOSE_GONE || event.code === INBOX_CLOSE_UNAUTHORIZED) {
                    if (refetched) end();
                    else void lookup(true);
                    return;
                }
                setStatus('connecting');
                clear();
                timer = setTimeout(() => dial(pointer, false), inboxRedialMs(++attempt));
            };
        };

        // Ask for the pointer; `again` is the gone-socket's one refetch.
        const lookup = async (again: boolean) => {
            clear();
            kickRef.current = null;
            let pointer: RunInboxPointer | null;
            try {
                pointer = await fetchRunInbox(channel, runId);
            } catch {
                pointer = null;
            }
            if (cancelled) return;
            if (pointer) {
                dial(pointer, again);
                return;
            }
            if (again || ++tries >= INBOX_RETRY_LIMIT) {
                end();
                return;
            }
            setStatus('pending');
            kickRef.current = () => void lookup(false);
            timer = setTimeout(() => void lookup(false), INBOX_RETRY_MS);
        };
        void lookup(false);

        return () => {
            cancelled = true;
            clear();
            kickRef.current = null;
            const socket = socketRef.current;
            socketRef.current = null;
            socket?.close();
        };
    }, [channel, runId, live]);

    // A phase change while the run has no inbox yet: ask again at once.
    useEffect(() => { kickRef.current?.(); }, [phase]);

    const answer = useCallback((id: string, optionId: string | null): boolean => {
        const socket = socketRef.current;
        if (!socket || socket.readyState !== WebSocket.OPEN) return false;
        socket.send(JSON.stringify(answerFrame(id, optionId)));
        setCards(c => c.filter(card => card.id !== id));
        return true;
    }, []);

    return { cards, status, answer };
}

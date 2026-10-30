// The annotation routes (api/routes/annotations.py): a cell's contexts and
// the user's writes to them, a run's file touches and its approval-card
// inbox. A refusal answers `{error: <code>, detail: <sentence>}` with a 4xx
// status; `annotationErrorCode` reads the code and `annotationErrorText`
// says it to the user.

import { describeApiError, requestJson } from './client';
import {
    normalizeAnnotationMessage, normalizeContext, normalizeTouch,
    type AnnotationContext, type AnnotationContextItem, type AnnotationMessage, type AnnotationMode, type FileTouch,
} from '@/lib/annotations';

export interface PostAnnotationBody {
    operation_id: string;
    context_id: string;
    parent_id?: string;
    body: string;
    context_items?: AnnotationContextItem[];
    mode: AnnotationMode;
    held?: boolean;
}

export interface PatchAnnotationBody {
    expected_revision: number;
    body?: string;
    context_items?: AnnotationContextItem[];
    mode?: AnnotationMode;
}

/** Where a live run's broadcast inbox attaches. */
export interface RunInboxPointer {
    chat_id: string;
    ws_path: string;
    token: string;
}

const base = (channel: string, cellId: string) =>
    `/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/annotations`;

const status = (error: unknown): number | undefined =>
    (error as { response?: { status?: number } })?.response?.status;

function message(raw: unknown): AnnotationMessage {
    const m = normalizeAnnotationMessage(raw);
    if (!m) throw new Error('The server answered with an unreadable message.');
    return m;
}

function context(raw: unknown): AnnotationContext {
    const c = normalizeContext(raw);
    if (!c) throw new Error('The server answered with an unreadable annotation context.');
    return c;
}

/** The cell's context, then every live, starting or stopping run's. */
export const fetchCellAnnotations = async (channel: string, cellId: string): Promise<AnnotationContext[]> => {
    const data = await requestJson<{ contexts?: unknown[] }>(base(channel, cellId), 'GET');
    return (data?.contexts ?? []).map(normalizeContext).filter((c): c is AnnotationContext => c !== null);
};

/** One context in any state (a sealed run's included); null when unknown. */
export const fetchAnnotationContext = async (channel: string, cellId: string, contextId: string): Promise<AnnotationContext | null> => {
    try {
        return context(await requestJson(`${base(channel, cellId)}/${encodeURIComponent(contextId)}`, 'GET'));
    } catch (error) {
        if (status(error) === 404) return null;
        throw error;
    }
};

export const postAnnotation = async (channel: string, cellId: string, body: PostAnnotationBody): Promise<AnnotationMessage> =>
    message(await requestJson(base(channel, cellId), 'POST', body));

export const patchAnnotation = async (channel: string, cellId: string, id: string, body: PatchAnnotationBody): Promise<AnnotationMessage> =>
    message(await requestJson(`${base(channel, cellId)}/${encodeURIComponent(id)}`, 'PATCH', body));

export const deleteAnnotation = async (channel: string, cellId: string, id: string, expectedRevision: number): Promise<void> => {
    await requestJson(`${base(channel, cellId)}/${encodeURIComponent(id)}?expected_revision=${expectedRevision}`, 'DELETE');
};

/** Move a cell-context message (and its replies) into a live run's context. */
export const sendAnnotationToRun = async (channel: string, cellId: string, id: string, runId: string): Promise<AnnotationMessage> =>
    message(await requestJson(`${base(channel, cellId)}/${encodeURIComponent(id)}/send`, 'POST', { run_id: runId }));

/** Reorder every unread top-level message; those from `heldFrom` on are held. */
export const orderAnnotations = async (channel: string, cellId: string, contextId: string, ids: string[], heldFrom?: number): Promise<AnnotationContext> =>
    context(await requestJson(`${base(channel, cellId)}/${encodeURIComponent(contextId)}/order`, 'PUT',
        heldFrom === undefined ? { ids } : { ids, held_from: heldFrom }));

export const setAnnotationBar = async (channel: string, cellId: string, contextId: string, on: boolean): Promise<AnnotationContext> =>
    context(await requestJson(`${base(channel, cellId)}/${encodeURIComponent(contextId)}/bar`, 'PUT', { on }));

/** Make these unread messages interrupts (moved to the head). */
export const interruptAnnotations = async (channel: string, cellId: string, contextId: string, ids: string[]): Promise<AnnotationContext> =>
    context(await requestJson(`${base(channel, cellId)}/${encodeURIComponent(contextId)}/interrupt`, 'POST', { ids }));

/** Every file the run's commits touched, oldest first. */
export const fetchFileTouches = async (channel: string, runId: string): Promise<FileTouch[]> => {
    const data = await requestJson<{ touches?: unknown[] }>(
        `/channels/${encodeURIComponent(channel)}/runs/${encodeURIComponent(runId)}/file_touches`, 'GET');
    return (data?.touches ?? []).map(normalizeTouch).filter((t): t is FileTouch => t !== null);
};

/** The run's broadcast inbox; null while it has none (404). */
export const fetchRunInbox = async (channel: string, runId: string): Promise<RunInboxPointer | null> => {
    try {
        const data = await requestJson<RunInboxPointer>(
            `/channels/${encodeURIComponent(channel)}/runs/${encodeURIComponent(runId)}/inbox`, 'GET');
        return data && typeof data.ws_path === 'string' && typeof data.token === 'string' ? data : null;
    } catch (error) {
        if (status(error) === 404) return null;
        throw error;
    }
};

/** A refusal's code (`response.data.error`), whatever its status; null for
 *  anything else. */
export function annotationErrorCode(error: unknown): string | null {
    const code = (error as { response?: { data?: { error?: unknown } } })?.response?.data?.error;
    return typeof code === 'string' ? code : null;
}

const NOTICE: Record<string, string> = {
    sealed: 'This run has ended; your message was not sent.',
    locked: 'The run has already read this message; it can no longer change.',
    revision: 'This message changed elsewhere; look again and retry.',
    parent_not_root: 'The message you answered is gone.',
    unknown_message: 'The message you answered is gone.',
    unknown_context: 'The message you answered is gone.',
    run_not_live: 'That run is no longer live.',
    stale: 'The queue changed elsewhere; look again and retry.',
    bar_off: 'Turn the queue bar on first.',
    too_large: 'The message is over 64 KiB.',
    empty_text: 'Write a message first.',
    full: 'This context holds 2000 messages; delete some first.',
    operation_reused: 'That message was already sent differently; send it again.',
    invalid: 'The server could not read that message.',
};

/** The sentence for a failed annotation write. */
export function annotationErrorText(error: unknown): string {
    const code = annotationErrorCode(error);
    if (code && NOTICE[code]) return NOTICE[code];
    const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    return describeApiError(error);
}

/** Codes after which the context is fetched again (it changed elsewhere). */
export const ANNOTATION_REFRESH_CODES: ReadonlySet<string> = new Set([
    'revision', 'locked', 'sealed', 'unknown_context', 'unknown_message', 'parent_not_root', 'stale',
]);

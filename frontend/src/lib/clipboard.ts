import { useCollimateStore } from '@/stores/useCollimateStore';

/** Put `text` on the system clipboard. A refusal (an insecure context, a
 *  denied permission) is said in an alert, in `opts.cellId`'s cell when
 *  given. Resolves whether the text was copied. */
export async function copyText(text: string, opts?: { cellId?: string }): Promise<boolean> {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch (error) {
        await useCollimateStore.getState().openAlert({
            title: 'Copy failed',
            body: `The clipboard refused the text${error instanceof Error && error.message ? ` (${error.message})` : ''}. Copy it by hand:\n${text}`,
            ...(opts?.cellId ? { cellId: opts.cellId } : {}),
        });
        return false;
    }
}

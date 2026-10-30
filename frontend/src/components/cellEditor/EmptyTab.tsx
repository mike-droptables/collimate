import { openFilesPanel } from '@/hooks/sceneKeyboardEffects';
import { btn } from '@/components/ui/formStyles';

/** An empty tab's body. Finding, opening and creating files is the Files
 *  panel's job (Search finds one, Browse/Create makes one in any folder),
 *  so the tab offers a way into that panel rather than a chooser of its
 *  own. A read-only cell takes no panel, so its button is disabled. */
export function EmptyTab({ cellId, readOnly }: { cellId: string; readOnly: boolean }) {
    return <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
        <button type="button" className={btn} disabled={readOnly}
            title={readOnly ? 'This cell is read-only' : undefined}
            onClick={() => openFilesPanel(cellId)}>Open existing or create a new file</button>
        <span className="text-[0.75rem] text-ink-3">…or drag a tab here</span>
    </div>;
}

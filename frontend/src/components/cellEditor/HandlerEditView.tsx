import { useEffect, useState } from 'react';
import { ArrowLeft } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { HandlerInfo } from '@/lib/definitionApi';
import { parseDefinition } from '@/lib/definitionFiles';
import { fetchTextBodies } from '@/lib/fileTransfer';
import { writeHandler } from '@/lib/handlerExamples';
import { hint } from '@/lib/handlerStatus';
import { revealCellFile } from '@/lib/revealFile';
import type { VirtualFile } from '@/lib/types';
import { DefinitionForm } from '@/components/definitions/DefinitionForm';
import { ScrollArea } from '@/components/ui/scroll-area';
import { btn, panelLabel } from '@/components/ui/formStyles';
import { escapeGoesBack } from '@/components/capabilities/manageActions';
import { cn } from '@/lib/utils';

/** The guidance a definition holds, to tell whether a save changed it. */
function guidanceOf(text: string): unknown {
    try { return parseDefinition(text).guidance ?? ''; } catch { return text; }
}

/**
 * A user handler's Edit: the form over its `.handler` (name, types,
 * guidance, examples), in place of the Capabilities manage view's list, as
 * the runner editor replaces it. Save writes the definition into its cell
 * only if it is still the one read (a revision check), then goes back to
 * the list, saying when new guidance needs a build to take effect. Esc
 * goes back.
 */
export function HandlerEditView({ handler, onBack, onSaved }: {
    handler: HandlerInfo;
    onBack: () => void;
    /** Saved: back to the list, with the card's notice. */
    onSaved: (notice: string) => void;
}) {
    const channel = useCollimateStore(s => s.activeChannel?.id ?? '');
    const [source, setSource] = useState<{ file: VirtualFile; text: string } | null>(null);
    const [error, setError] = useState('');

    // The definition as it is when the edit opens: a later change by anyone
    // else makes Save refuse rather than reload the form under the edit.
    useEffect(() => {
        let live = true;
        const file = useCollimateStore.getState().locateCell(handler.cell_id)?.cell.files.find(item => item.name === handler.definition_path);
        (file ? fetchTextBodies(channel, handler.cell_id, [file]).then(bodies => bodies.get(file.name)) : Promise.resolve(undefined))
            .then(text => {
                if (!live) return;
                if (!file) setError('The handler definition is missing.');
                else if (text === undefined) setError('The handler definition is unavailable.');
                else setSource({ file, text });
            })
            .catch(e => { if (live) setError(e instanceof Error ? e.message : 'Could not open the handler'); });
        return () => { live = false; };
    }, [channel, handler.cell_id, handler.definition_path]);

    const save = async (text: string) => {
        if (!source) return;
        await writeHandler(handler, text, source.file);
        onSaved(guidanceOf(text) === guidanceOf(source.text) ? 'Saved.' : 'Saved. Build again to use new guidance.');
    };

    return <div data-handler-editor="" className="flex min-h-0 flex-1 flex-col" onKeyDown={e => escapeGoesBack(e, onBack)}>
        <div className="flex h-10 flex-none items-center gap-2 border-b border-line px-3.5">
            <button type="button" className={cn(btn, 'border-transparent px-1.5')} onClick={onBack}>
                <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
                <span className={panelLabel}>Back to Capabilities</span>
            </button>
            <span className="min-w-0 flex-1 truncate text-[0.8125rem] text-ink">Edit ‘{handler.name}’</span>
            <button type="button" className={btn} title={hint('openFile', handler)}
                onClick={() => revealCellFile(handler.cell_id, handler.definition_path)}>Open file</button>
        </div>
        {error && <p role="alert" className="px-3.5 pt-2 text-[0.75rem] text-red">{error}</p>}
        <ScrollArea className="min-h-0 flex-1">
            <div className="px-3.5 pt-3 pb-5 text-[0.8125rem]">
                {source
                    ? <DefinitionForm kind="handler" content={source.text} readOnly={false} onSave={save} />
                    : !error && <p className="text-[0.78125rem] text-ink-3">Loading the handler…</p>}
            </div>
        </ScrollArea>
    </div>;
}

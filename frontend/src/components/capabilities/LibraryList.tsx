import type { LibraryEntry } from '@/lib/api/capabilitiesStack';
import { btn, card, chip } from '@/components/ui/formStyles';

/** Who names a library: the cells whose cell.yaml lists it under `shared:`. */
function namedBy(library: LibraryEntry, cellName: (cellId: string | undefined) => string): string {
    const names = library.named_by.map(cellId => cellName(cellId) || 'a cell').filter(Boolean);
    return names.length ? `Named by ${names.join(', ')}` : 'No cell names it yet';
}

function LibraryCard({ library, cellName, onShowCell }: {
    library: LibraryEntry;
    cellName: (cellId: string | undefined) => string;
    onShowCell?: (cellId: string) => void;
}) {
    const cellId = library.cell_id;
    return <li className={`${card} flex flex-col gap-1 px-3 py-2.5`} data-library={library.id}>
        <div className="flex flex-wrap items-center gap-2">
            <span className="text-[0.8125rem] font-medium text-ink">{library.name}</span>
            <span className={`${chip} font-mono`}>{library.id}</span>
            {library.version && <span className="text-[0.71875rem] text-ink-3">v{library.version}</span>}
        </div>
        {library.description && <p className="text-[0.75rem] text-ink-2">{library.description}</p>}
        <p className="text-[0.71875rem] text-ink-3">
            {library.app ? 'The app’s copy, since no Capabilities cell holds it. ' : cellId ? `In ‘${cellName(cellId)}’. ` : ''}
            {namedBy(library, cellName)}.
        </p>
        {!library.app && cellId && onShowCell && <div className="flex gap-2 pt-0.5">
            <button type="button" className={btn} onClick={() => onShowCell(cellId)}>Open cell</button>
        </div>}
    </li>;
}

/**
 * The Libraries group's body: each library a Capabilities cell holds, then
 * each library of the app that a cell names under `shared:` while the
 * channel holds none of that id (its generators get the app's copy; adding
 * the library from the catalog puts one on Capabilities). A run stages each
 * named library beside its generator.
 */
export function LibraryList({ libraries, app, cellName, onShowCell }: {
    libraries: LibraryEntry[];
    app: LibraryEntry[];
    cellName: (cellId: string | undefined) => string;
    onShowCell?: (cellId: string) => void;
}) {
    return <>
        {libraries.length > 0 && <ul className="flex flex-col gap-2">
            {libraries.map(library => <LibraryCard key={`${library.cell_id}:${library.id}`} library={library}
                cellName={cellName} onShowCell={onShowCell} />)}
        </ul>}
        {app.length > 0 && <div className="flex flex-col gap-2" data-app-libraries="">
            <p className="px-0.5 text-[0.75rem] text-ink-2">From the app ({app.length})</p>
            <ul className="flex flex-col gap-2">
                {app.map(library => <LibraryCard key={`app:${library.id}`} library={library} cellName={cellName} />)}
            </ul>
        </div>}
    </>;
}

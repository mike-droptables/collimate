import { flushFileDocuments } from '@/lib/fileDocuments';
import { flushCellSaves } from '@/lib/cellSaveBarrier';
import { fetchTextBodies, withFetchedBodies } from '@/lib/fileTransfer';
import { definitionText, parseDefinition, replaceFiles, textFile } from '@/lib/definitionFiles';
import { handlerForPath } from '@/lib/handlerTypes';
import { isFolderKeep, remintEmptiedParent } from '@/lib/fileTree';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { BuildInspection, ExampleDeclaration, HandlerInfo } from '@/lib/definitionApi';
import type { Cell, VirtualFile } from '@/lib/types';

/** One example file's verdict against what its example expects (`path`
 *  null when the example could not be checked at all). */
export interface ExampleVerdict { path: string | null; pass: boolean; message: string }

/** Each declared example's verdicts by its folder: a file whose verdict is
 *  the expected one matches; an example the checks could not run on fails
 *  with why. */
export function compareExamples(inspection: BuildInspection): Map<string, ExampleVerdict[]> {
    return new Map(inspection.examples.map(example => [example.folder, example.error
        ? [{ path: null, pass: false, message: example.error }]
        : example.verdicts.map(verdict => {
            const actual = verdict.valid ? 'valid' : 'invalid';
            const pass = actual === example.expect;
            return {
                path: verdict.path,
                pass,
                message: `${pass ? 'Matches' : 'Mismatch'}: expected ${example.expect}, got ${actual}${verdict.error ? ` — ${verdict.error}` : ''}`,
            };
        })]));
}

/** How many examples match (every file of the example does), of how many. */
export function matchCount(verdicts: ReadonlyMap<string, ExampleVerdict[]>): { matched: number; total: number } {
    const all = [...verdicts.values()];
    return { matched: all.filter(files => files.length > 0 && files.every(file => file.pass)).length, total: all.length };
}

/** The cells an example can come from: every cell of the channel but the
 *  built-in cards, each with its files of a type the handler covers. The
 *  handler's own folder never offers its own files. */
export function exampleSources(cells: readonly Cell[], handler: HandlerInfo): { cell: Cell; paths: string[] }[] {
    const own = `${handler.folder}/`;
    return cells.filter(cell => !cell.builtin).map(cell => ({
        cell,
        paths: cell.files.map(file => file.name)
            .filter(path => !isFolderKeep(path) && !(cell.id === handler.cell_id && path.startsWith(own)) && handlerForPath([handler], path) !== null)
            .sort(),
    })).filter(source => source.paths.length > 0);
}

/** A user handler's example folder in its cell. */
export const examplePrefix = (handler: Pick<HandlerInfo, 'folder'>, folder: string) => `${handler.folder}/examples/${folder}/`;

const channelId = () => useCollimateStore.getState().activeChannel?.id ?? '';

/** The handler's definition file and its parsed manifest, as its cell holds
 *  them now. */
async function readDefinition(handler: HandlerInfo): Promise<{ definition: VirtualFile; manifest: Record<string, unknown> }> {
    const definition = useCollimateStore.getState().locateCell(handler.cell_id)?.cell.files.find(file => file.name === handler.definition_path);
    if (!definition) throw new Error('The handler cell is unavailable.');
    const body = (await fetchTextBodies(channelId(), handler.cell_id, [definition])).get(definition.name);
    if (body === undefined) throw new Error('The handler definition is unavailable.');
    return { definition, manifest: parseDefinition(body) };
}

/** Write the handler's definition (with any files beside it, and without the
 *  `removed` ones) into its cell, refusing when the definition changed since
 *  `expected` was read. */
export async function writeHandler(handler: HandlerInfo, text: string, expected: VirtualFile,
    additions: VirtualFile[] = [], removed: (path: string) => boolean = () => false): Promise<void> {
    const store = useCollimateStore.getState();
    const target = store.locateCell(handler.cell_id)?.cell;
    if (!target) throw new Error('The handler cell is unavailable.');
    const latest = target.files.find(file => file.name === handler.definition_path);
    if (!latest || latest.file_revision !== expected.file_revision || latest.body_hash !== expected.body_hash) {
        throw new Error('The handler definition changed. Reopen it and retry.');
    }
    const kept = target.files.filter(file => !removed(file.name));
    if (!await store.applyCellFiles(handler.cell_id, replaceFiles(kept, [textFile(handler.definition_path, text), ...additions]))) {
        throw new Error('Could not save the handler.');
    }
}

const examplesOf = (manifest: Record<string, unknown>): Record<string, unknown>[] =>
    Array.isArray(manifest.examples) ? manifest.examples as Record<string, unknown>[] : [];

/** Copy `paths` of `sourceCellId` (their current bytes) into a new example
 *  folder of user handler `handler`, `<folder>/examples/example-xxxxxxxx/`,
 *  and declare it in the handler's definition. Answers the folder. */
export async function addUserExample(handler: HandlerInfo, sourceCellId: string, paths: readonly string[], declaration: ExampleDeclaration): Promise<string> {
    await flushCellSaves(sourceCellId); await flushFileDocuments();
    const source = useCollimateStore.getState().locateCell(sourceCellId)?.cell;
    if (!source) throw new Error('The example’s cell is unavailable.');
    const chosen = source.files.filter(file => paths.includes(file.name));
    const load = withFetchedBodies(chosen, await fetchTextBodies(channelId(), sourceCellId, chosen));
    const { definition, manifest } = await readDefinition(handler);
    const folder = `example-${crypto.randomUUID().slice(0, 8)}`;
    const copies = chosen.map(file => {
        const now = useCollimateStore.getState().locateCell(sourceCellId)?.cell.files.find(item => item.name === file.name);
        const loaded = now && load(now);
        if (!loaded) throw new Error('A selected file changed while its bytes were loading. Retry.');
        return { ...loaded, name: `${examplePrefix(handler, folder)}${file.name}` };
    });
    manifest.examples = [...examplesOf(manifest), { folder, ...declaration }];
    await writeHandler(handler, definitionText(manifest), definition, copies);
    return folder;
}

/** Change a user handler example's description, verdict or feedback. */
export async function patchUserExample(handler: HandlerInfo, folder: string, fields: ExampleDeclaration): Promise<void> {
    const { definition, manifest } = await readDefinition(handler);
    const examples = examplesOf(manifest);
    if (!examples.some(example => example.folder === folder)) throw new Error('The example is no longer in the handler.');
    manifest.examples = examples.map(example => (example.folder === folder ? { ...example, ...fields } : example));
    await writeHandler(handler, definitionText(manifest), definition);
}

/** Remove a user handler's example: its declaration and its folder. */
export async function removeUserExample(handler: HandlerInfo, folder: string): Promise<void> {
    const { definition, manifest } = await readDefinition(handler);
    manifest.examples = examplesOf(manifest).filter(example => example.folder !== folder);
    const prefix = examplePrefix(handler, folder);
    await writeHandler(handler, definitionText(manifest), definition, [], path => path.startsWith(prefix));
}

/** The cell's files without the handler's folder: Delete. A parent folder
 *  the handler leaves empty keeps its marker. */
export function withoutHandler(files: VirtualFile[], handler: Pick<HandlerInfo, 'folder'>): VirtualFile[] {
    const prefix = `${handler.folder}/`;
    return remintEmptiedParent(files.filter(file => !file.name.startsWith(prefix)), handler.folder);
}

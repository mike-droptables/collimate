// A run's deliberate replacement of a newer revision: after a conflict
// refusal told the run that someone saved a path, the run wrote over that
// revision by naming it (the SDK's `replace_revision`). The version it wrote
// records the revision and its writer; History and the run's file touches
// show it, so the replaced save is never a silent overwrite (its bytes are
// the Manual Edit version just before).

/** Who wrote a revision (`persistence.writer_actor`). */
export interface RevisionWriter {
    kind: 'user' | 'host' | 'run';
    run_id?: string;
    label?: string;
    generator?: string | null;
}

export interface Replacement {
    path: string;
    revision: number;
    writer_id: string | null;
    writer: RevisionWriter | null;
    /** The revision of `path` the run's write became, or null. */
    file_revision: number | null;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

function normalizeWriter(raw: unknown): RevisionWriter | null {
    if (!isRecord(raw) || (raw.kind !== 'user' && raw.kind !== 'host' && raw.kind !== 'run')) return null;
    const writer: RevisionWriter = { kind: raw.kind };
    if (typeof raw.run_id === 'string') writer.run_id = raw.run_id;
    if (typeof raw.label === 'string') writer.label = raw.label;
    if (typeof raw.generator === 'string' || raw.generator === null) writer.generator = raw.generator;
    return writer;
}

/** A wire replacement mark (`{revision, writer_id, writer}`, with `path`
 *  or the touch's own `path`), or null when it is not one. */
export function normalizeReplacement(raw: unknown, path?: string): Replacement | null {
    if (!isRecord(raw)) return null;
    const at = typeof raw.path === 'string' ? raw.path : path;
    const revision = raw.revision;
    if (!at || typeof revision !== 'number' || !Number.isInteger(revision) || revision < 1) return null;
    const accepted = raw.file_revision;
    return {
        path: at, revision,
        writer_id: typeof raw.writer_id === 'string' ? raw.writer_id : null,
        writer: normalizeWriter(raw.writer),
        file_revision: typeof accepted === 'number' && Number.isInteger(accepted) && accepted > 0 ? accepted : null,
    };
}

/** The marks a history version carries, valid ones only. */
export const versionReplacements = (raw: unknown): Replacement[] =>
    Array.isArray(raw) ? raw.map(r => normalizeReplacement(r)).filter((r): r is Replacement => r !== null) : [];

/** "your revision 3", with ``of`` (" of notes.md") after the revision. */
function whose(r: Replacement, of = ''): string {
    switch (r.writer?.kind) {
        case 'user': return `your revision ${r.revision}${of}`;
        case 'host': return `the host's revision ${r.revision}${of}`;
        case 'run': return `revision ${r.revision}${of} by run ${r.writer.label ?? r.writer.run_id ?? ''}`.trimEnd();
        default: return `revision ${r.revision}${of}`;
    }
}

/** "replaced your revision 3 deliberately" (a file touch names its path). */
export const replacementNote = (r: Replacement): string => `replaced ${whose(r)} deliberately`;

/** "Replaced your revision 3 of notes.md deliberately". */
export const replacementText = (r: Replacement): string => `Replaced ${whose(r, ` of ${r.path}`)} deliberately`;

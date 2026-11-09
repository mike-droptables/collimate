import { useEffect, useState } from 'react';

import { fileReplacement, isRunWriter, type FileReplacement } from '@/lib/fileReplacement';
import { replacementNote } from '@/lib/replacements';

/** "Run agent-x replaced your revision 3 deliberately" over a file whose
 *  shown revision is a run's acknowledged write over a newer save; the
 *  replaced save stays in History as the version before it. */
export function FileReplacementNote({ channel, cellId, path, revision, writerId }: {
    channel: string; cellId: string; path: string; revision: number; writerId: string | null | undefined;
}) {
    const [found, setFound] = useState<FileReplacement | null>(null);
    useEffect(() => {
        setFound(null);
        if (!channel || !cellId || !isRunWriter(writerId) || !(revision > 0)) return;
        let active = true;
        void fileReplacement(channel, cellId, path, revision).then(r => { if (active) setFound(r); }, () => undefined);
        return () => { active = false; };
    }, [channel, cellId, path, revision, writerId]);
    if (!found) return null;
    const who = found.run ? `Run ${found.run}` : 'A run';
    return <div data-testid="file-replaced" role="note"
        className="flex-none border-b border-line bg-brass-soft px-3 py-2 text-[0.78125rem] text-ink">
        {who} {replacementNote(found.mark)}. The replaced save is kept in History as the version before it.
    </div>;
}

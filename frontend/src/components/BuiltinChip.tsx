import { chip } from '@/components/ui/formStyles';
import { definitionKind } from '@/lib/definitionFiles';
import type { Cell } from '@/lib/types';
import { cn } from '@/lib/utils';

/** What every built-in card's chip says on hover. */
const FIXED = 'Part of the app: it can’t be removed, moved or edited.';
/** Added only on a card that holds handlers (the Definitions card): its
 *  handlers take added examples and regenerated skills. */
const HANDLER_ADDITIONS = 'Add examples and regenerate skills from its card in Capabilities.';

/** The chip's hover text for a built-in card with these files. */
function builtinChipTitle(files: Cell['files']): string {
    return files.some(file => definitionKind(file.name) === 'handler') ? `${FIXED} ${HANDLER_ADDITIONS}` : FIXED;
}

/** The quiet "Built-in" chip on a built-in card's head (the cell header and
 *  its overview card), or on one of the app's handlers (`title` says what
 *  being the app's means there). */
export function BuiltinChip(props: { cell: Pick<Cell, 'files'> } | { title: string }) {
    const title = 'title' in props ? props.title : builtinChipTitle(props.cell.files);
    return <span data-builtin-chip="" className={cn(chip, 'flex-none')} title={title}>Built-in</span>;
}

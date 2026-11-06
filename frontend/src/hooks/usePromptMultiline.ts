import { useState } from 'react';

/** Whether a runner's prompt box is the several-line one, decided once per
 *  mount: a value that has held a line break keeps the textarea for the rest
 *  of the edit, so deleting its last break never swaps the control for an
 *  `<input>` (a new element, which drops focus and the caret mid-edit). */
export function usePromptMultiline(value: string): boolean {
    const [latched, setLatched] = useState(() => value.includes('\n'));
    const multiline = latched || value.includes('\n');
    if (multiline && !latched) setLatched(true);
    return multiline;
}

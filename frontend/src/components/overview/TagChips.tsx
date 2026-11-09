import { useLayoutEffect, useRef, type HTMLAttributes } from 'react';
import { cn } from '@/lib/utils';

// A cell's tags as quiet read-only chips (hairline line-2 border, no fill):
// one line on a shelf card (RemovedCard, the rest clipped), or wrapping on
// a board card while the shelf's tag toggle is on (OverviewCard). Wrapped,
// the row keeps to two lines so the card keeps its fixed height: the chips
// that would start a third line are hidden and a last "+N" chip counts
// them, its title naming them. Nothing for no tags.

const chipClass = 'rounded-sm border border-line-2 px-1.5 text-[0.6875rem] leading-[1rem] text-ink-2';

export interface TagChipsProps extends Omit<HTMLAttributes<HTMLUListElement>, 'children'> {
    tags: readonly string[];
    /** Wrap onto two lines at most, counting the rest; else one line. */
    wrap?: boolean;
}

/**
 * Fit a wrapping row to its two-line cap (the row's own max height): show
 * every chip, hide the first that ends below the cap and all after it, then
 * show the "+N" chip and hide one more chip at a time until it fits too.
 * The visibility and the count are the DOM's alone (React renders every
 * chip without `hidden`, and the "+N" chip hidden and empty), so a rerender
 * never undoes the fit; a new tag list or a new width refits from scratch.
 */
function fitTwoLines(row: HTMLUListElement) {
    const chips = [...row.querySelectorAll<HTMLElement>(':scope > [data-tag-chip]')];
    const more = row.querySelector<HTMLElement>(':scope > [data-tag-more]');
    if (!more) return;
    for (const chip of chips) chip.hidden = false;
    more.hidden = true;
    const limit = row.clientHeight;
    const fits = (el: HTMLElement) => el.offsetTop + el.offsetHeight <= limit + 0.5;
    let shown = chips.findIndex(chip => !fits(chip));
    if (shown === -1) return;
    for (const chip of chips.slice(shown)) chip.hidden = true;
    more.hidden = false;
    const count = () => {
        more.textContent = `+${chips.length - shown}`;
        more.title = chips.slice(shown).map(chip => chip.textContent).join(', ');
    };
    count();
    while (shown > 0 && !fits(more)) {
        shown -= 1;
        chips[shown].hidden = true;
        count();
    }
}

export function TagChips({ tags, wrap = false, className, ...rest }: TagChipsProps) {
    const rowRef = useRef<HTMLUListElement>(null);
    const tagsKey = tags.join('\n');

    useLayoutEffect(() => {
        const row = rowRef.current;
        if (!wrap || !row) return;
        fitTwoLines(row);
        if (typeof ResizeObserver === 'undefined') return;
        let width = row.clientWidth;
        const observer = new ResizeObserver(() => {
            if (row.clientWidth === width) return;
            width = row.clientWidth;
            fitTwoLines(row);
        });
        observer.observe(row);
        return () => observer.disconnect();
    }, [wrap, tagsKey]);

    if (tags.length === 0) return null;
    return (
        <ul
            ref={rowRef}
            aria-label="Tags"
            {...rest}
            className={cn(
                'flex list-none gap-1 overflow-hidden',
                // Two lines: a chip is 18px tall (16px line, 1px borders), 4px
                // apart. Relative, so the chips' offsetTop is the row's.
                wrap ? 'relative max-h-[2.5rem] flex-wrap' : 'whitespace-nowrap',
                className,
            )}
        >
            {tags.map(tag => (
                <li
                    key={tag}
                    data-tag-chip
                    title={wrap ? tag : undefined}
                    className={cn(chipClass, wrap ? 'min-w-0 max-w-full truncate' : 'flex-none')}
                >
                    {tag}
                </li>
            ))}
            {wrap && <li data-tag-more hidden className={cn(chipClass, 'flex-none')} />}
        </ul>
    );
}

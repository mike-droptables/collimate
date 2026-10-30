/** The document's root font size in CSS px: the interface's native size
 *  (globals.css sets 110%), which every rem-sized measure scales with. Browser
 *  tests derive their expected px from it rather than pinning a root size. */
export const rem = (): number => parseFloat(getComputedStyle(document.documentElement).fontSize);

/** `n` rem in CSS px. */
export const px = (n: number): number => n * rem();

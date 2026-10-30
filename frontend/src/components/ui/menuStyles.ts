/** The menu vocabulary (the mock's `.menu`): the surface every canvas menu
 *  renders on (the cell, tab and file context menus, their Open with /
 *  Compare with submenus, the stack menu) and its rows, so one never drifts
 *  from the next. Position, z and width stay with each menu. */
// `transition-none`: `duration-100` times the entry animation but also sets
// transition-duration, and with the default transition-property (`all`) a
// menu's measured placement would glide in from wherever it was first
// rendered instead of appearing in place.
export const menuSurface = 'bg-sheet border border-line-2 rounded-lg p-1 shadow-(--shadow) text-[0.8125rem] transition-none animate-in fade-in zoom-in-95 duration-100';

const row = 'flex w-full items-center gap-2.5 h-[1.875rem] px-2.5 rounded-md text-left text-ink-2 outline-none transition-colors aria-disabled:opacity-40 aria-disabled:pointer-events-none disabled:opacity-40 disabled:pointer-events-none';
/** A menu row: 30px, its icon in ink-3, the hover fill on sheet-2. */
export const menuItem = `${row} [&_svg]:text-ink-3 hover:bg-sheet-2 hover:text-ink focus-visible:bg-sheet-2 focus-visible:text-ink`;
/** A destructive menu row: red with the soft red fill on hover. */
export const menuItemDanger = `${row} [&_svg]:text-ink-3 hover:bg-red-soft hover:text-red hover:[&_svg]:text-red focus-visible:bg-red-soft focus-visible:text-red`;
/** The hairline between menu groups. */
export const menuSep = 'h-px mx-1.5 my-1 bg-line';
/** A menu group's heading. */
export const menuHead = 'px-2.5 pt-2 pb-[0.3125rem] text-[0.6875rem] font-semibold uppercase tracking-[0.08em] text-ink-3';

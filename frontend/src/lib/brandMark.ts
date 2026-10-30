// The app's brand mark in its 24-unit box: three brass rays entering from
// the left, a lens at the centre, and a small filled triangle where they
// converge on the right. MarkGlyph draws it in the top chrome, the landing's
// nav mark inlines it, and public/favicon.svg draws it in its 64 box at
// x1.75 + 11 (build/render_icons.py renders every icon from that file).

/** The mark's lens: a ring on the middle ray at the 24 box's centre, about
 *  a third of the mark's height so it still reads as a ring at 18px. */
export const MARK_LENS = { cx: 12, cy: 12, r: 3.2 } as const;

/** The mark's three rays (from 3,6 / 3,12 / 3,18 toward the tip on the
 *  right), each cut where it passes behind the lens: every round cap stops
 *  0.9 (half a stroke) clear of the ring's stroke, so the rays read as
 *  passing through the lens rather than under a dot. The favicon's rays lie
 *  on the same lines but are cut wider, keeping that clearance for its
 *  heavier stroke (4.2 in 64, 2.4 here). */
export const MARK_RAYS = 'M3 6 8.11 7.56M3 12 6.1 12M3 18 7.89 16.23';

/** The tip: a small filled triangle right of the lens, its apex at the
 *  point the rays converge on (21.6, 12). Filled, not stroked, so it keeps
 *  its point at every size (stroked, converging rays merged into a blob at
 *  icon sizes). */
export const MARK_TIP = 'M17.3 9.8 21.6 12 17.3 14.2Z';

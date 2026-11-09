// The selection edge: how heavy the selected card's brass edge is, set per
// skin (ThemeScene.edge, written as SCENE_CSS_VARS.edge on <html>).

import type { CSSProperties } from 'react';
import { SCENE_CSS_VARS } from './themes';

/** The skin's edge width, 1px before the first skin applies. */
const EDGE = `var(${SCENE_CSS_VARS.edge}, 1px)`;

/** The selected card's edge past its 1px brass border: an outline of the
 *  rest of the width, flush outside the border box and following its
 *  radius, so the card's box and everything in it (renderer frames
 *  included) stay where they are as the selection moves. Nothing at 1px. */
export const SELECTED_EDGE_STYLE: CSSProperties = {
    outline: `calc(${EDGE} - 1px) solid var(--brass)`,
    outlineOffset: 0,
};

/** The open tab's top rule: one px heavier than the card edge in every
 *  cell, so a selection change recolours it and never moves a tab's label. */
export const TAB_RULE_CLASS = 'border-t-[length:calc(var(--selection-edge,1px)+1px)]';

import { expect, it } from 'vitest';
import { SCENE_CSS_VARS } from './themes';
import { SELECTED_EDGE_STYLE, TAB_RULE_CLASS } from './selectionEdge';

it('the selected card adds the rest of the skin\'s edge width outside its 1px border, in brass', () => {
    expect(SELECTED_EDGE_STYLE).toEqual({
        outline: `calc(var(${SCENE_CSS_VARS.edge}, 1px) - 1px) solid var(--brass)`,
        outlineOffset: 0,
    });
});

it('the open tab\'s rule reads the same width, one px heavier', () => {
    expect(TAB_RULE_CLASS).toBe(`border-t-[length:calc(var(${SCENE_CSS_VARS.edge},1px)+1px)]`);
});

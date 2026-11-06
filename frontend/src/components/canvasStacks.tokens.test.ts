// The canvas and its stacks (cards, heads, fans, drag overlays, the new-cell
// drop slot, the stack menu (ui/Menu) and width popup, the channel scrubber,
// the empty and loading states)
// paint only from the skin tokens: no retired palette name, no shadcn
// variable, no Tailwind palette utility and no colour literal may come back
// into these sources, in any branch of any render.

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

const here = path.dirname(fileURLToPath(import.meta.url));

const FILES = [
    'StackItem.tsx',
    'StackGroup.tsx',
    'StackWidthControl.tsx',
    'ui/Menu.tsx',
    'StackCellScrubber.tsx',
    'FanFrame.tsx',
    'CardEdgeResize.tsx',
    'StackScene.tsx',
    'SceneDragOverlays.tsx',
    'OverviewCardPreview.tsx',
    'ChannelScrubber.tsx',
    'CellDiffZone.tsx',
    'StackApp.tsx',
    'BlurredTopBar.tsx',
    'EmptyChannelNewCell.tsx',
    'CommandPane.tsx',
    'NewCellSlotGhost.tsx',
];

const RETIRED = RETIRED_TOKENS;
const SHADCN = /var\(--(?:primary|secondary|muted|accent|background|foreground|card|popover|border|input|ring|destructive)\b|\b(?:bg|text|border|ring)-(?:primary|secondary|muted|accent|background|foreground|card|popover|destructive)\b/;
const PALETTE = /\b(?:bg|text|border|ring|from|to|via|fill|stroke|accent|shadow)-(?:slate|zinc|gray|neutral|stone|red|amber|green|emerald|blue|sky|teal|pink|rose)-\d{2,3}\b|\b(?:bg|text|border|ring)-(?:white|black)\b/;
const LITERAL = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|color-mix\(|\bshadow-(?:sm|md|lg|xl|2xl)\b/;

describe('canvas and stacks token sweep', () => {
    it.each(FILES)('%s names no retired token, shadcn variable, palette utility or colour literal (S04)', file => {
        const source = readFileSync(path.join(here, file), 'utf8');
        expect(source).not.toMatch(RETIRED);
        expect(source).not.toMatch(SHADCN);
        expect(source).not.toMatch(PALETTE);
        expect(source).not.toMatch(LITERAL);
    });

    it.each(FILES)('%s draws from the skin tokens (S04)', file => {
        const source = readFileSync(path.join(here, file), 'utf8');
        expect(source).toMatch(/\b(?:text|bg|border|ring)-(?:canvas|sheet(?:-[23])?|line(?:-2)?|ink(?:-[23])?|teal(?:-soft|-sheet)?|red(?:-soft)?|green|brass(?:-soft)?|code-bg)\b|var\(--(?:canvas|sheet|line|ink|teal|brass|red|shadow|lift)|formStyles|menuStyles/);
    });
});

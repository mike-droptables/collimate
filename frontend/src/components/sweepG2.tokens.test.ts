import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

// Sweep G2 (the cell editor: tabs, Files pane, the status pane, the dock
// preview, history, the in-cell panels, and the Capabilities manage card
// with its handler, renderer and generator rows) paints only on the skin
// tokens: a source scan that fails the moment an old palette name, a colour
// literal, a Tailwind palette utility, a white/black utility or a stock
// shadow comes back into one of these files.
const FILES = [
    'CellEditor.tsx',
    'RunStopButton.tsx',
    'cellEditor/DockPreview.tsx',
    'cellEditor/RunsPane.tsx',
    'cellEditor/PaneResizeHandle.tsx',
    'cellEditor/TabStrip.tsx',
    'cellEditor/FilesPane.tsx',
    'cellEditor/HistorySections.tsx',
    'cellEditor/HistoryRail.tsx',
    'cellEditor/EmptyTab.tsx',
    'cellEditor/RendererCard.tsx',
    'cellEditor/TabContextMenu.tsx',
    'cellEditor/CellDiffBanner.tsx',
    'cellEditor/TabBody.tsx',
    'cellEditor/ContentPane.tsx',
    'cellEditor/CompactPinnedPreview.tsx',
    'cellPanel/CellPanel.tsx',
    'cellPanel/FilesPanel.tsx',
    'cellEditor/HandlerManagement.tsx',
    'cellEditor/HandlerCard.tsx',
    'cellEditor/HandlerExamples.tsx',
    'cellEditor/handlerSteps.tsx',
    'cellEditor/HandlerEditView.tsx',
    'cellEditor/GeneratorRows.tsx',
    'runner/RunnerPicker.tsx',
    ...readdirSync(join(__dirname, 'capabilities')).filter(file => file.endsWith('.tsx')).map(file => `capabilities/${file}`),
    ...readdirSync(join(__dirname, 'runsPane')).filter(file => file.endsWith('.tsx') && !file.includes('.test.')).map(file => `runsPane/${file}`),
    'RunStatusIndicator.tsx',
];

const PALETTE = 'slate|zinc|gray|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose';
const FORBIDDEN: Array<[string, RegExp]> = [
    ['old token name', RETIRED_TOKENS],
    ['old palette word', /\b(?:amber|emerald|pink)\b/],
    ['hex colour', /#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{1}|[0-9a-fA-F]{3}|[0-9a-fA-F]{5})?\b/],
    ['functional colour', /\b(?:rgba?|hsla?|oklch)\(|color-mix\(/],
    ['palette utility', new RegExp(`\\b[a-z-]*-(?:${PALETTE})-\\d{2,3}\\b`)],
    ['white/black utility', /\b(?:bg|text|border|ring|fill|stroke|from|to|via)-(?:white|black)\b/],
    ['stock shadow', /\bshadow-(?:xs|sm|md|lg|xl|2xl|inner)\b/],
];

describe('Sweep G2 token sweep', () => {
    it.each(FILES)('%s carries no old token, colour literal or palette utility (S04)', (file) => {
        const src = readFileSync(join(__dirname, file), 'utf8');
        for (const [what, re] of FORBIDDEN) {
            const hit = src.match(re);
            expect(hit?.[0], `${file}: ${what}`).toBeUndefined();
        }
    });
});

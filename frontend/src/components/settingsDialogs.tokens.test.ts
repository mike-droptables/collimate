// The Commands pane's settings view and its sections, the dialogs, the
// full-screen states and the Capabilities manage card paint only from the
// skin tokens: no retired palette name, no Tailwind palette utility and no
// colour literal may come back into these sources, in any branch of any
// render.

import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

const here = path.dirname(fileURLToPath(import.meta.url));

const FILES = [
    'settings/KeyManager.tsx',
    'settings/EditingSection.tsx',
    'settings/ThemeSection.tsx',
    'settings/ServersSection.tsx',
    'settings/ToggleRow.tsx',
    'commandPane/SettingsView.tsx',
    'commandPane/SettingsTransferRows.tsx',
    'commandPane/PaneFooter.tsx',
    'commandPane/paneStyles.ts',
    'CommandPane.tsx',
    'ChannelDialog.tsx',
    'ConfirmDialog.tsx',
    'ChannelCreateView.tsx',
    'channelCreate/SuggestedTab.tsx',
    'channelCreate/ConfigTab.tsx',
    'channelCreate/SourceSection.tsx',
    'ApiSettingsModal.tsx',
    'ServerDownScreen.tsx',
    'ShutdownOverlay.tsx',
    'cellEditor/HandlerManagement.tsx',
    'cellEditor/HandlerCard.tsx',
    'cellEditor/HandlerExamples.tsx',
    'cellEditor/handlerSteps.tsx',
    'cellEditor/HandlerEditView.tsx',
    ...readdirSync(path.join(here, 'capabilities')).filter(file => file.endsWith('.tsx')).map(file => `capabilities/${file}`),
];

const PALETTE = /\b(?:bg|text|border|ring|from|to|via|fill|stroke|accent)-(?:slate|zinc|gray|neutral|stone|red|amber|green|emerald|blue|sky|teal|pink|rose)-\d{2,3}\b|\b(?:bg|text|border)-(?:white|black)\b/;
const LITERAL = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|color-mix\(|shadow-(?:sm|md|lg|xl|2xl)\b/;

describe('settings and dialogs token sweep', () => {
    it.each(FILES)('%s names no retired token, palette utility or colour literal (S04)', file => {
        const source = readFileSync(path.join(here, file), 'utf8');
        expect(source).not.toMatch(RETIRED_TOKENS);
        expect(source).not.toMatch(PALETTE);
        expect(source).not.toMatch(LITERAL);
    });

    it.each(FILES)('%s draws from the skin token utilities (S04)', file => {
        const source = readFileSync(path.join(here, file), 'utf8');
        expect(source).toMatch(/\b(?:text|bg|border)-(?:canvas|sheet(?:-[23])?|line(?:-2)?|ink(?:-[23])?|teal|red|green|brass|code-bg)\b|formStyles/);
    });
});

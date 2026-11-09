import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

// Sweep G6 (views, overlays, renderer hosts, ui primitives) paints only on
// the skin tokens: a source scan that fails the moment an old palette name,
// a colour literal, a Tailwind palette utility or a shadcn theme utility
// (none of which the skins define) comes back into one of these files.
const FILES = [
    'MarkdownView.tsx',
    'cellEditor/CompareTabBody.tsx',
    'FullFileEditor.tsx',
    'CompareWithSubmenu.tsx',
    'OpenWithSubmenu.tsx',
    'NativeRenderer.tsx',
    'SandboxIframe.tsx',
    'StandaloneSandboxIframe.tsx',
    'ui/IconPicker.tsx',
    'ui/button.tsx',
    'ui/modal.tsx',
    'ui/switch.tsx',
    'ui/editable-label.tsx',
    'ui/input.tsx',
];

const PALETTE = 'slate|zinc|gray|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose';
const FORBIDDEN: Array<[string, RegExp]> = [
    ['old token name', RETIRED_TOKENS],
    ['hex colour', /#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{1}|[0-9a-fA-F]{3}|[0-9a-fA-F]{5})?\b/],
    ['functional colour', /\b(?:rgba?|hsla?|oklch)\(/],
    ['palette utility', new RegExp(`\\b[a-z-]*-(?:${PALETTE})-\\d{2,3}\\b`)],
    ['white/black utility', /\b(?:bg|text|border|ring|fill|stroke)-(?:white|black)\b/],
    ['shadcn theme utility', /\b(?:bg|text|border|ring|placeholder:text)-(?:primary|secondary|destructive|background|foreground|accent|muted|input|ring|popover|card)(?:-foreground)?\b/],
    ['stock shadow', /\bshadow-(?:xs|sm|md|lg|xl|2xl|inner)\b/],
];

describe('Sweep G6 token sweep', () => {
    it.each(FILES)('%s carries no old token, colour literal or palette utility (S04)', (file) => {
        const src = readFileSync(join(__dirname, file), 'utf8');
        for (const [what, re] of FORBIDDEN) {
            const hit = src.match(re);
            expect(hit?.[0], `${file}: ${what}`).toBeUndefined();
        }
    });
});

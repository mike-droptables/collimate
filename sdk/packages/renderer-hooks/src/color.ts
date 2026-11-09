// SPDX-License-Identifier: MIT
import type { RendererTheme, ThemeVarName } from './types';

// ---------------------------------------------------------------------------
// resolveVar / toHex8 — concrete values for libraries that cannot read CSS
// variables (Monaco's defineTheme, Mermaid's themeVariables, xterm).
// ---------------------------------------------------------------------------

/** A token's resolved value from a snapshot, or '' when it has none. */
export function resolveVar(theme: RendererTheme, name: ThemeVarName): string {
    const value = theme && theme.vars ? theme.vars[name] : undefined;
    return typeof value === 'string' ? value.trim() : '';
}

const hex2 = (n: number): string =>
    Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0');

function channel(raw: string): number | null {
    const t = raw.trim();
    if (/^[-+]?(\d+\.?\d*|\.\d+)%$/.test(t)) return (parseFloat(t) / 100) * 255;
    if (/^[-+]?(\d+\.?\d*|\.\d+)$/.test(t)) return parseFloat(t);
    return null;
}

function alpha(raw: string | undefined): number | null {
    if (raw === undefined) return 255;
    const t = raw.trim();
    if (/^[-+]?(\d+\.?\d*|\.\d+)%$/.test(t)) return (parseFloat(t) / 100) * 255;
    if (/^[-+]?(\d+\.?\d*|\.\d+)$/.test(t)) return parseFloat(t) * 255;
    return null;
}

/**
 * A CSS colour as `#rrggbbaa`, or null when it is not a hex or rgb()/rgba()
 * colour. Accepts `#rgb`, `#rgba`, `#rrggbb`, `#rrggbbaa`, and rgb()/rgba()
 * in comma or space syntax (`rgb(1 2 3 / 50%)`).
 */
export function toHex8(cssColor: string): string | null {
    const c = (cssColor || '').trim().toLowerCase();
    let m = /^#([0-9a-f]{3,8})$/.exec(c);
    if (m) {
        const h = m[1];
        if (h.length === 3 || h.length === 4) {
            const full = h.split('').map((d) => d + d).join('');
            return '#' + (full.length === 6 ? full + 'ff' : full);
        }
        if (h.length === 6) return '#' + h + 'ff';
        if (h.length === 8) return '#' + h;
        return null;
    }
    m = /^rgba?\(([^)]*)\)$/.exec(c);
    if (!m) return null;
    const body = m[1].trim();
    let parts: string[];
    let a: string | undefined;
    if (body.includes(',')) {
        parts = body.split(',');
        if (parts.length === 4) a = parts.pop();
    } else {
        const [rgb, slash] = body.split('/');
        parts = rgb.trim().split(/\s+/);
        a = slash;
    }
    if (parts.length !== 3) return null;
    const rgb = parts.map(channel);
    const al = alpha(a);
    if (rgb.some((v) => v === null) || al === null) return null;
    return '#' + (rgb as number[]).map(hex2).join('') + hex2(al);
}

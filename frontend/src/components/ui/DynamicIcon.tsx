import { createElement } from 'react';
import type { LucideIcon } from 'lucide-react';
import { resolveIcon, DEFAULT_GROUP_ICON } from '@/lib/iconRegistry';

interface DynamicIconProps {
    /** Registry name (lucide-style kebab). Falls back to `fallback` when
     *  unknown/null. */
    name?: string | null;
    className?: string;
    fallback?: LucideIcon;
    'aria-hidden'?: boolean;
    'aria-label'?: string;
}

/** Render a lucide glyph resolved from the icon registry by name.
 *
 *  Centralizes the resolve-with-fallback so callers render `<DynamicIcon />`
 *  as ordinary JSX instead of assigning a resolved component to a capitalized
 *  local (which trips the `react-hooks/static-components` lint) or repeating
 *  the `?? DEFAULT_GROUP_ICON` fallback. The one `createElement` lives here. */
export function DynamicIcon({ name, className, fallback = DEFAULT_GROUP_ICON, ...rest }: DynamicIconProps) {
    return createElement(resolveIcon(name) ?? fallback, { className, ...rest });
}

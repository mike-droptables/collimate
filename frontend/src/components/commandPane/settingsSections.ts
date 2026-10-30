// The settings view's sections: one table read by the root's rows, the
// commands view's Settings group and a section's head (its title and
// one-line description).

import type { LucideIcon } from 'lucide-react';
import { Key, Palette, Server, SpellCheck } from 'lucide-react';
import type { SettingsSection } from '@/stores/collimate/commandPaneSlice';

export interface SettingsSectionDef {
    id: SettingsSection;
    label: string;
    description: string;
    icon: LucideIcon;
    /** Offered only inside the desktop shell (multi-server is desktop-only). */
    desktopOnly?: boolean;
}

export const SETTINGS_SECTIONS: readonly SettingsSectionDef[] = [
    { id: 'keys', label: 'Key manager', description: 'Secrets are stored in the server’s system keyring, separate from channel files.', icon: Key },
    { id: 'editing', label: 'Editing', description: "Spellcheck in this channel's fields, in the app and every renderer", icon: SpellCheck },
    { id: 'theme', label: 'Themes', description: 'Colors, radius and fonts', icon: Palette },
    { id: 'servers', label: 'Servers', description: 'One server per window; the local server keeps running', icon: Server, desktopOnly: true },
];

/** The sections a page offers: the desktop-only ones only with `desktop`. */
export function settingsSectionsFor(desktop: boolean): SettingsSectionDef[] {
    return SETTINGS_SECTIONS.filter(s => desktop || !s.desktopOnly);
}

/** A section's entry, when the page offers it. */
export function settingsSectionOf(id: SettingsSection, desktop: boolean): SettingsSectionDef | undefined {
    return settingsSectionsFor(desktop).find(s => s.id === id);
}

/** On every row of the settings root (the sections, the Settings file
 *  rows): ↑↓ step between them. A section's row carries its id. */
export const SETTINGS_ROW_ATTR = 'data-settings-row';

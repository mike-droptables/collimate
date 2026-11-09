// The settings sections: one table read by the Commands pane's settings
// root, its commands view's Settings group and a section's head (its title
// and one-line description), and by the channels page's Settings tab and
// docked panel, which share the Accounts entry's words.
//
// Each section lives at one scope. Accounts are this machine's (the server
// computer's keyring, shared by every channel, server and app profile on
// it); a theme override is the open channel's. The server's default theme
// and this profile's own preferences are set only on the channels page's
// Settings tab. The library's themes are edited from either the page's
// Default theme panel or a channel's override section.

import type { LucideIcon } from 'lucide-react';
import { Key, Palette } from 'lucide-react';
import { desktopBridge } from '@/lib/desktop';
import type { SettingsSection } from '@/stores/collimate/commandPaneSlice';

export interface SettingsSectionDef {
    id: SettingsSection;
    label: string;
    /** The one-line description, as a head shows it now (the Accounts one
     *  names a remote server's computer). */
    describe: () => string;
    icon: LucideIcon;
    /** Needs an open channel (the sceneless pane over the channels page
     *  leaves it out). */
    channelOnly?: boolean;
}

/** The accounts' title and what they hold, and where: the tab row, the
 *  panel head, the pane section head and the runner editor's dialog all
 *  say it in these words. */
export const ACCOUNTS_TITLE = 'Accounts on this machine';
export const ACCOUNTS_DESCRIPTION = 'Model accounts, logins and Git entries, kept in the keyring of the computer this server runs on. Every channel, server and app profile there shares them.';

/** The Accounts description as a head shows it. In a desktop window on a
 *  remote server "this machine" is that server's computer, so it says so;
 *  a browser cannot tell, and the base words hold either way. */
export function accountsDescription(): string {
    const server = desktopBridge()?.server;
    return server?.kind === 'remote' ? `${ACCOUNTS_DESCRIPTION} That is ${server.name}’s computer, not this one.` : ACCOUNTS_DESCRIPTION;
}

const OVERRIDE_DESCRIPTION = "This channel's theme; the default and the themes themselves are this server's, shared by every channel.";

export const SETTINGS_SECTIONS: readonly SettingsSectionDef[] = [
    { id: 'keys', label: ACCOUNTS_TITLE, describe: accountsDescription, icon: Key },
    { id: 'theme', label: 'Override theme for this channel', describe: () => OVERRIDE_DESCRIPTION, icon: Palette, channelOnly: true },
];

/** The sections a pane offers: the channel's own only with `channel`. */
export function settingsSectionsFor(channel: boolean): SettingsSectionDef[] {
    return SETTINGS_SECTIONS.filter(s => channel || !s.channelOnly);
}

/** A section's entry, when the pane offers it. */
export function settingsSectionOf(id: SettingsSection, channel: boolean): SettingsSectionDef | undefined {
    return settingsSectionsFor(channel).find(s => s.id === id);
}

/** On every row of the settings root: ↑↓ step between them. A row carries
 *  its section's id. */
export const SETTINGS_ROW_ATTR = 'data-settings-row';

/** What the channels page's Settings tab opens in its docked panel. */
export type PageSettingsEntry = 'keys' | 'theme';

/** A page entry's title and description: its tab row and its panel head
 *  say the same words. */
export function pageSettingsHead(entry: PageSettingsEntry): { title: string; description: string } {
    return entry === 'keys'
        ? { title: ACCOUNTS_TITLE, description: accountsDescription() }
        : { title: 'Default theme', description: 'The theme of this page and of every channel that does not override it. Themes are shared by every channel on this server.' };
}

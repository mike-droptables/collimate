// The channels page's Settings tab: the settings that are not one
// channel's, each at its own scope, top to bottom.
//
// - Accounts on this machine: the server computer's keyring, shared by
//   every channel, server and app profile there. The row opens the page's
//   docked settings panel (SettingsPanel) on the Key manager.
// - Default theme: this server's theme library and the default every
//   channel without an override follows (and this page shows). The row
//   names the default and opens the panel on the theme editor.
// - This profile, inline: this app's preferences on this computer (the
//   desktop profile's settings.json) or, in a browser, this browser's for
//   this server (its localStorage). Short, the only per-profile part, and
//   never in the Commands pane.
//
// A channel's own settings (its theme override, its keyboard) are the
// Commands pane's.

import { useId } from 'react';
import type { LucideIcon } from 'lucide-react';
import { ChevronRight, Key, Palette } from 'lucide-react';
import { cn } from '@/lib/utils';
import { desktopBridge, type CollimateDesktop } from '@/lib/desktop';
import { themeDefOf, themeLabel } from '@/lib/userThemes';
import { useThemeContext } from '@/contexts/ThemeContext';
import { ProfileSection } from '@/components/settings/ProfileSection';
import { card, sectionLabel } from '@/components/ui/formStyles';
import { pageSettingsHead, type PageSettingsEntry } from '@/components/commandPane/settingsSections';

/** Where This profile's preferences live, in words: the desktop app
 *  profile the shell names (desktop/src/main/profile.ts), or this browser.
 *  A shell too old to name its profile gets the sentence without a name. */
function profileSentence(shell: CollimateDesktop | undefined): string {
    if (!shell) return 'This browser, for this server';
    const name = typeof shell.getProfileName === 'function' ? shell.getProfileName() : '';
    return name ? `This app on this computer: the ${name} profile` : 'This app on this computer';
}

export function SettingsTab({ onOpen }: { onOpen: (entry: PageSettingsEntry) => void }) {
    const { defaultThemeId, userThemes } = useThemeContext();
    const defaultLabel = themeLabel(themeDefOf(defaultThemeId, userThemes));
    return <div className="flex flex-col gap-5">
        <div className="flex flex-col gap-2">
            <EntryRow entry="keys" icon={Key} onOpen={onOpen} />
            <EntryRow entry="theme" icon={Palette} value={defaultLabel} onOpen={onOpen} />
        </div>
        <section aria-labelledby="settings-this-profile" className="flex flex-col gap-3">
            <div>
                <h2 id="settings-this-profile" className={sectionLabel}>This profile</h2>
                <p data-profile-sentence className="mt-1 text-[0.78125rem] text-ink-2">
                    {profileSentence(desktopBridge())}
                </p>
            </div>
            <ProfileSection />
        </section>
    </div>;
}

/** A row opening the docked panel: the entry's icon, title and
 *  description, its current value (the default theme's name), a chevron.
 *  Named by its title; the value, then the description, describe it (as
 *  the Commands pane's settings rows do). */
function EntryRow({ entry, icon: Icon, value, onOpen }: {
    entry: PageSettingsEntry; icon: LucideIcon; value?: string; onOpen: (entry: PageSettingsEntry) => void;
}) {
    const { title, description } = pageSettingsHead(entry);
    const id = useId();
    const describedBy = value ? `${id}-value ${id}-description` : `${id}-description`;
    return <button type="button" data-settings-entry={entry} aria-label={title} aria-describedby={describedBy} onClick={() => onOpen(entry)}
        className={cn(card, 'group flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-sheet-2')}>
        <Icon className="size-4 flex-none text-ink-3 group-hover:text-ink-2" strokeWidth={1.6} />
        <span className="flex min-w-0 flex-1 flex-col">
            <span className="text-[0.8125rem] font-medium text-ink">{title}</span>
            <span id={`${id}-description`} className="text-[0.75rem] text-ink-3">{description}</span>
        </span>
        {value && <span id={`${id}-value`} data-settings-value className="max-w-[12rem] flex-none truncate text-[0.78125rem] text-ink-2">{value}</span>}
        <ChevronRight className="size-4 flex-none text-ink-3" strokeWidth={1.6} />
    </button>;
}

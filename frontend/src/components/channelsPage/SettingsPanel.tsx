// The channels page's docked settings panel: the Settings tab's Accounts on
// this machine and Default theme, and the New tab's accounts, open here.
// It is the Commands pane's column (PaneColumn, layered inside the page)
// docked on the pane's side, holding a settings section's chrome
// (SettingsSectionFrame) with the KeyManager or the theme editor in its
// default mode, so the page and the pane read alike.
//
// Docked, not modal: it is a complementary landmark, a press elsewhere on
// the page leaves it open (it may hold a half-made account), and a tab
// switch keeps it. The X and Esc close it; Esc first goes to whatever owns
// the keyboard inside it (KeyManager's own dialogs and menus). It takes the
// keyboard when it opens (the X, or with a draft the draft's first field)
// and ChannelsPage gives it back to whatever opened it when it closes.

import { lazy, useEffect, useRef, type KeyboardEvent } from 'react';
import { X } from 'lucide-react';
import { KeyManager } from '@/components/settings/KeyManager';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { KEYBOARD_OWNER_ATTR } from '@/lib/keyboardOwner';
import type { CustomEntryDraft } from '@/lib/keystoreStructure';
import { PaneColumn } from '@/components/commandPane/PaneColumn';
import { SettingsSectionFrame } from '@/components/commandPane/SettingsView';
import { PANE_HEAD_BUTTON, SETTINGS_HEAD_ROW } from '@/components/commandPane/paneStyles';
import { pageSettingsHead } from '@/components/commandPane/settingsSections';

// The theme editor loads when the panel first shows it.
const DefaultThemeBody = lazy(() => import('@/components/settings/ThemeSection').then(module => ({
    default: () => <module.ThemeSection mode="default" />,
})));

/** What the panel shows: the accounts (with a draft, on a new custom entry
 *  shaped like a runner's role) or the server's default theme. */
export type SettingsPanelKind = { kind: 'keys'; draft?: CustomEntryDraft } | { kind: 'theme' };

/** Something inside the panel that answers Esc itself: a keyboard owner or
 *  a modal (the page's own root is aria-modal, and lies under the panel). */
const OWNS_ESC = `[${KEYBOARD_OWNER_ATTR}], [aria-modal="true"]:not([data-channel-create])`;

const FIELD = 'input:not([type=hidden]), textarea, select';

export function SettingsPanel({ panel, onClose, onAccountsChanged }: {
    panel: SettingsPanelKind;
    onClose: () => void;
    /** Each account change the KeyManager makes (the New tab, still in view
     *  beside the panel, reads the accounts again). */
    onAccountsChanged?: () => void;
}) {
    const side = useCollimateStore(s => s.commandPane.side);
    const { title, description } = pageSettingsHead(panel.kind);
    const columnRef = useRef<HTMLElement>(null);
    const closeRef = useRef<HTMLButtonElement>(null);
    const draft = panel.kind === 'keys' ? panel.draft : undefined;

    // The keyboard comes here on open: the X, or the draft's first field
    // once the KeyManager has drawn its form.
    useEffect(() => {
        const column = columnRef.current;
        if (!draft || !column) { closeRef.current?.focus(); return; }
        const focusField = () => {
            const field = column.querySelector('[data-settings-section]')?.querySelector<HTMLElement>(FIELD) ?? null;
            field?.focus();
            return field !== null;
        };
        if (focusField()) return;
        const watch = new MutationObserver(() => { if (focusField()) watch.disconnect(); });
        watch.observe(column, { childList: true, subtree: true });
        return () => watch.disconnect();
    }, [panel.kind, draft]);

    const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
        if (e.key !== 'Escape' || e.defaultPrevented) return;
        if (e.target instanceof Element && e.target.closest(OWNS_ESC)) return;
        e.preventDefault();
        onClose();
    };

    return (
        <PaneColumn
            side={side}
            layer="page"
            title="Settings"
            label={title}
            paneRef={columnRef}
            dataAttrs={{ 'data-settings-panel': panel.kind }}
            onKeyDown={onKeyDown}
            headExtra={
                <button ref={closeRef} type="button" aria-label={`Close ${title}`} title="Close (Esc)" onClick={onClose}
                    className={`${PANE_HEAD_BUTTON} mt-[0.1875rem]`}>
                    <X className="size-3.5" strokeWidth={1.6} />
                </button>
            }
        >
            <SettingsSectionFrame
                id={panel.kind}
                description={description}
                escLabel="close"
                onEsc={onClose}
                head={<div className={`${SETTINGS_HEAD_ROW} px-3.5`}>
                    <h2 className="flex-1 min-w-0 truncate text-[0.8125rem] font-medium text-ink">{title}</h2>
                </div>}
            >
                {panel.kind === 'keys' ? <KeyManager draft={draft} onChanged={onAccountsChanged} /> : <DefaultThemeBody />}
            </SettingsSectionFrame>
        </PaneColumn>
    );
}

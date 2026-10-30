// The Commands pane's settings view, in the pane's column under its name
// row. The root lists the sections (icon, label, the section's one-line
// description) and the Settings file rows; ↑↓ step between them and ↵ opens
// one. A section shows a head with the way back and its title, its
// description, and its body, loaded when first shown. Esc is the pane's
// (a section → the root → the commands).

import { lazy, Suspense, useId, useRef } from 'react';
import { ChevronLeft } from 'lucide-react';
import { kbd } from '@/components/ui/formStyles';
import { is } from '@/lib/keybindings/handlers';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import type { SettingsSection } from '@/stores/collimate/commandPaneSlice';
import { PaneFooter } from './PaneFooter';
import { PANE_GROUP_HEADING } from './paneStyles';
import { SettingsTransferRows } from './SettingsTransferRows';
import { PANE_ENTRY_ATTR } from '@/lib/commandPane';
import { SETTINGS_ROW_ATTR, settingsSectionOf, settingsSectionsFor } from './settingsSections';

// The section bodies load only when a section is first opened.
const KeyManager = lazy(() => import('@/components/settings/KeyManager').then(module => ({ default: module.KeyManager })));
const EditingSection = lazy(() => import('@/components/settings/EditingSection').then(module => ({ default: module.EditingSection })));
const ThemeSection = lazy(() => import('@/components/settings/ThemeSection').then(module => ({ default: module.ThemeSection })));
const ServersSection = lazy(() => import('@/components/settings/ServersSection').then(module => ({ default: module.ServersSection })));

const BODIES: Record<SettingsSection, React.ComponentType> = {
    keys: KeyManager,
    editing: EditingSection,
    theme: ThemeSection,
    servers: ServersSection,
};

export interface SettingsViewProps {
    /** The open section, or null for the root. One the page does not
     *  offer shows the root. */
    section: SettingsSection | null;
    /** Inside the desktop shell: the desktop-only sections show. */
    desktop: boolean;
    /** The section the root came back from: its row takes the keyboard. */
    from: SettingsSection | null;
    onOpen: (section: SettingsSection) => void;
    /** One level up: a section → the root → the commands. */
    onBack: () => void;
}

const headRow = 'flex-none h-9 flex items-center gap-2 border-b border-line';

export function SettingsView({ section, desktop, from, onOpen, onBack }: SettingsViewProps) {
    const open = section ? settingsSectionOf(section, desktop) : undefined;
    return open
        ? <SectionView key={open.id} id={open.id} desktop={desktop} onBack={onBack} />
        : <RootView desktop={desktop} from={from} onOpen={onOpen} onBack={onBack} />;
}

function RootView({ desktop, from, onOpen, onBack }: Omit<SettingsViewProps, 'section'>) {
    const listRef = useRef<HTMLDivElement>(null);
    const descId = useId();
    const sections = settingsSectionsFor(desktop);
    const entry = sections.some(s => s.id === from) ? from : sections[0]?.id;

    // ↑↓ step between the rows (wrapping); ↵ is the focused row's own click.
    const onKeyDown = (e: React.KeyboardEvent) => {
        const next = is(e.nativeEvent, SHORTCUT_IDS.LIST_NEXT);
        if (!next && !is(e.nativeEvent, SHORTCUT_IDS.LIST_PREV)) return;
        e.preventDefault();
        const rows = [...(listRef.current?.querySelectorAll<HTMLElement>(`[${SETTINGS_ROW_ATTR}]:not(:disabled)`) ?? [])];
        if (rows.length === 0) return;
        const at = rows.indexOf(document.activeElement as HTMLElement);
        const to = at < 0 ? (next ? 0 : rows.length - 1) : (at + (next ? 1 : -1) + rows.length) % rows.length;
        rows[to].focus();
    };

    return (
        <>
            <div className={`${headRow} px-3.5`}>
                <h2 className="flex-1 min-w-0 truncate text-[0.8125rem] font-medium text-ink">Settings</h2>
                <kbd className={kbd}>esc</kbd>
            </div>
            <div ref={listRef} onKeyDown={onKeyDown} className="flex-1 min-h-0 overflow-y-auto px-1.5 pt-1 pb-2.5">
                {sections.map(s => {
                    const Icon = s.icon;
                    return (
                        <button
                            key={s.id}
                            type="button"
                            {...{ [SETTINGS_ROW_ATTR]: s.id, ...(s.id === entry && { [PANE_ENTRY_ATTR]: '' }) }}
                            aria-label={s.label}
                            aria-describedby={`${descId}-${s.id}`}
                            onClick={() => onOpen(s.id)}
                            className="group w-full flex items-center gap-2.5 h-11 px-2 rounded-md text-left text-ink-2 outline-none hover:bg-sheet-2 hover:text-ink focus:bg-sheet-2 focus:text-ink"
                        >
                            <Icon className="size-3.5 flex-none text-ink-3 group-focus:text-ink-2" strokeWidth={1.6} />
                            <span className="flex-1 min-w-0 flex flex-col">
                                <span className="truncate text-[0.8125rem] leading-[1.125rem]">{s.label}</span>
                                <span id={`${descId}-${s.id}`} className="truncate text-[0.75rem] leading-4 text-ink-3">{s.description}</span>
                            </span>
                        </button>
                    );
                })}
                <h4 className={PANE_GROUP_HEADING}>Settings file</h4>
                <SettingsTransferRows />
            </div>
            <PaneFooter hints={[['↑↓', 'move'], ['↵', 'open']]} escLabel="back" onEsc={onBack} />
        </>
    );
}

function SectionView({ id, desktop, onBack }: { id: SettingsSection; desktop: boolean; onBack: () => void }) {
    const def = settingsSectionOf(id, desktop)!;
    const Body = BODIES[id];
    return (
        <>
            <div className={`${headRow} pl-1.5 pr-3.5`}>
                <button
                    type="button"
                    {...{ [PANE_ENTRY_ATTR]: '' }}
                    aria-label="Back to settings"
                    onClick={onBack}
                    className="flex-none inline-flex items-center gap-0.5 h-6 pl-0.5 pr-1.5 rounded-xs text-[0.78125rem] text-ink-3 outline-none hover:text-ink hover:bg-sheet-2 focus-visible:text-ink focus-visible:bg-sheet-2"
                >
                    <ChevronLeft className="size-3.5" strokeWidth={1.6} />
                    Settings
                </button>
                <h2 className="flex-1 min-w-0 truncate text-[0.8125rem] font-medium text-ink">{def.label}</h2>
            </div>
            <p className="flex-none px-3.5 py-2 border-b border-line text-[0.75rem] text-ink-3">{def.description}</p>
            <div data-settings-section={id} className="flex-1 min-h-0 overflow-y-auto px-3.5 py-3">
                <Suspense fallback={null}>
                    <Body />
                </Suspense>
            </div>
            <PaneFooter hints={[]} escLabel="back" onEsc={onBack} />
        </>
    );
}

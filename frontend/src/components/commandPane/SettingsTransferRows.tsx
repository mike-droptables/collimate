// The settings root's Settings file rows: Export settings / Import settings.
// The file is the whole channel settings doc; an import replaces every
// section the file carries, then reloads the settings store so every
// consumer adopts it.

import { useRef, useState } from 'react';
import { Download, Upload, type LucideIcon } from 'lucide-react';
import { fetchChannelSettings, putChannelSettings } from '@/lib/api/settings';
import { RENDERER_PREFS_CHANGED_EVENT } from '@/lib/api/catalog';
import { parseSettingsImport, saveJsonFile, serializeSettings, settingsFileName } from '@/lib/settingsTransfer';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { SETTINGS_ROW_ATTR } from './settingsSections';

const errorText = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

function TransferRow({ icon: Icon, label, title, disabled, onClick }: {
    icon: LucideIcon; label: string; title: string; disabled: boolean; onClick: () => void;
}) {
    return (
        <button
            type="button"
            {...{ [SETTINGS_ROW_ATTR]: '' }}
            disabled={disabled}
            title={title}
            onClick={onClick}
            className="w-full flex items-center gap-2.5 h-[1.875rem] px-2 rounded-md text-left text-[0.8125rem] text-ink-2 outline-none hover:bg-sheet-2 hover:text-ink focus:bg-sheet-2 focus:text-ink disabled:opacity-50 disabled:hover:bg-transparent disabled:hover:text-ink-2"
        >
            <Icon className="size-3.5 flex-none text-ink-3" strokeWidth={1.6} />
            <span className="flex-1 min-w-0 truncate">{label}</span>
        </button>
    );
}

export function SettingsTransferRows() {
    const channel = useCollimateStore(s => s.activeChannel);
    const inputRef = useRef<HTMLInputElement>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');

    const exportSettings = async () => {
        if (!channel) return;
        setBusy(true); setError(''); setNotice('');
        try {
            const doc = await fetchChannelSettings(channel.id);
            if (await saveJsonFile(settingsFileName(channel.name), serializeSettings(doc))) setNotice('Settings exported.');
        } catch (e) {
            setError(errorText(e, 'Could not export the settings.'));
        } finally { setBusy(false); }
    };

    const importSettings = async (file: File) => {
        if (!channel) return;
        setBusy(true); setError(''); setNotice('');
        try {
            const parsed = parseSettingsImport(await file.text());
            if (!parsed.ok) { setError(parsed.error); return; }
            await putChannelSettings(channel.id, parsed.doc);
            await useChannelSettingsStore.getState().reloadAfterExternalWrite();
            // The renderer sections live outside the settings store: the
            // resolver cache and the management view reload on this event.
            window.dispatchEvent(new Event(RENDERER_PREFS_CHANGED_EVENT));
            setNotice('Settings imported.');
        } catch (e) {
            setError(errorText(e, 'Could not import the settings.'));
        } finally { setBusy(false); }
    };

    return (
        <>
            <TransferRow icon={Download} label="Export settings" title="Save this channel's settings as a .json file"
                disabled={busy || !channel} onClick={() => void exportSettings()} />
            <TransferRow icon={Upload} label="Import settings" title="Replace this channel's settings with a .json file"
                disabled={busy || !channel} onClick={() => inputRef.current?.click()} />
            <input
                ref={inputRef}
                type="file"
                accept="application/json,.json"
                aria-label="Settings file"
                tabIndex={-1}
                className="hidden"
                onChange={e => {
                    const file = e.target.files?.[0];
                    e.target.value = '';
                    if (file) void importSettings(file);
                }}
            />
            {error && <p role="alert" className="px-2 pt-1 text-[0.75rem] text-red">{error}</p>}
            {notice && !error && <p role="status" className="px-2 pt-1 text-[0.75rem] text-ink-3">{notice}</p>}
        </>
    );
}

import { useEffect, useId, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { cn } from '@/lib/utils';
import { btn, card, sectionLabel, segmentOff, segmentOn } from '@/components/ui/formStyles';
import { desktopBridge, type BuildInfo, type CollimateDesktop, type UpdateCheckStart } from '@/lib/desktop';
import {
    INTERFACE_SCALES, InterfaceScale, canSetInterfaceScale,
    getInterfaceScale, interfaceScaleLabel, setInterfaceScale,
} from '@/lib/interfaceScale';
import { EditingSection } from './EditingSection';
import { ToggleRow } from './ToggleRow';

// ─── This profile ────────────────────────────────────────────────────────────
// The body of "This profile" on the channels page's Settings tab: what
// belongs to the profile the page runs in, never to a channel or a server.
// In the desktop shell (one Electron profile, Collimate or Collimate Dev)
// that is the app's version and build stamp (the bridge's buildInfo()), the
// automatic-updates switch (the shell's settings.json), Check for updates
// (the same updater call as the menu's Check for Updates…; its answers are
// the shell's native dialogs, and only what happened at once shows here) and
// the interface size (the window's page zoom); remote servers update on
// their own machines. Everywhere, a browser included, the Editing group:
// spellcheck and opening the status section when a run starts, kept in the
// shell's settings.json or this browser's localStorage (lib/profilePrefs).

const message = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

export function ProfileSection() {
    const shell = desktopBridge();
    return (
        <div data-profile-section="" className="space-y-5">
            {shell && <AppIdentity info={shell.buildInfo()} />}
            {shell && <Updates shell={shell} />}
            {shell && canSetInterfaceScale() && <InterfaceSizeRow />}
            <section className="space-y-2" aria-label="Editing">
                <h4 className={sectionLabel}>Editing</h4>
                <EditingSection />
            </section>
        </div>
    );
}

/** The build stamp as one line: the commit, whether the tree had uncommitted
 *  changes, when it was packaged (in the reader's locale) and the server
 *  environment's hash; a development run has no stamp. */
function buildLine(info: BuildInfo): string {
    if (!info.commit) return 'No build stamp (a development run)';
    const parts = [`Build ${info.commit.slice(0, 10)}`];
    if (info.dirty) parts.push('uncommitted changes');
    if (info.builtAt) {
        const at = new Date(info.builtAt);
        parts.push(`built ${Number.isNaN(at.getTime()) ? info.builtAt : at.toLocaleString()}`);
    }
    if (info.serverEnvironmentHash) parts.push(`server environment ${info.serverEnvironmentHash.slice(0, 12)}`);
    return parts.join(' · ');
}

function AppIdentity({ info }: { info: BuildInfo }) {
    return (
        <section className="space-y-2" aria-label="App">
            <h4 className={sectionLabel}>App</h4>
            <div className={cn(card, 'px-3 py-2.5')}>
                <div className="text-[0.8125rem] font-medium text-ink">Collimate {info.version}</div>
                <p data-build-stamp className="mt-0.5 text-[0.75rem] text-ink-3">{buildLine(info)}</p>
            </div>
        </section>
    );
}

/** What a check did at once, as a sentence. */
function startText(start: UpdateCheckStart): string {
    switch (start.status) {
        case 'started':
            return 'Checking… the answer opens in a dialog.';
        case 'busy':
            return 'A check is already running; its answer opens in a dialog.';
        case 'unavailable':
            return `This install does not update itself: ${start.reason}.`;
    }
}

function Updates({ shell }: { shell: CollimateDesktop }) {
    const [automatic, setAutomatic] = useState<boolean | null>(null);
    const [saving, setSaving] = useState(false);
    const [checking, setChecking] = useState(false);
    const [status, setStatus] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let live = true;
        shell.getAutomaticUpdates().then(
            on => { if (live) setAutomatic(on); },
            e => { if (live) setError(message(e, 'The desktop shell did not answer.')); },
        );
        return () => { live = false; };
    }, [shell]);

    const toggle = async (next: boolean) => {
        setSaving(true);
        setError(null);
        try {
            setAutomatic(await shell.setAutomaticUpdates(next));
        } catch (e) {
            setError(message(e, 'The desktop shell refused that.'));
        } finally {
            setSaving(false);
        }
    };

    const check = async () => {
        setChecking(true);
        setStatus(null);
        setError(null);
        try {
            setStatus(startText(await shell.checkForUpdates()));
        } catch (e) {
            setError(message(e, 'The desktop shell refused that.'));
        } finally {
            setChecking(false);
        }
    };

    return (
        <section className="space-y-2" aria-label="Updates">
            <h4 className={sectionLabel}>Updates</h4>
            <ToggleRow
                icon={<RefreshCw className="size-3.5" strokeWidth={1.6} />}
                label="Automatic updates"
                desc="Check at startup, download in the background and offer to restart. Off: nothing until you check."
                checked={automatic ?? false}
                disabled={automatic === null || saving}
                onChange={next => void toggle(next)}
            />
            <div className="flex flex-wrap items-center gap-2">
                <button type="button" className={btn} disabled={checking} onClick={() => void check()}>
                    Check for updates
                </button>
                {status && <p role="status" className="text-[0.75rem] text-ink-2">{status}</p>}
            </div>
            {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
        </section>
    );
}

/** The scale the page zoom `factor` stands for, if it is one. */
const scaleAt = (factor: number): InterfaceScale | undefined =>
    INTERFACE_SCALES.find(scale => Math.abs(scale - factor) < 0.005);

/** This profile's interface size, as a segmented row. It follows a change
 *  made elsewhere (the View menu) through the shell's zoom signal. */
function InterfaceSizeRow() {
    const labelId = useId();
    const [scale, setScale] = useState<InterfaceScale | null>(() => getInterfaceScale());
    useEffect(() => desktopBridge()?.onZoomChanged((factor) => {
        setScale(scaleAt(factor) ?? getInterfaceScale());
    }), []);
    const choose = (next: InterfaceScale) => {
        if (setInterfaceScale(next)) setScale(next);
    };
    return (
        <section className="space-y-2" data-interface-size="">
            <h4 id={labelId} className={sectionLabel}>Interface size</h4>
            <div role="group" aria-labelledby={labelId} className="flex flex-wrap gap-1">
                {INTERFACE_SCALES.map(option => (
                    <button
                        key={option}
                        type="button"
                        aria-pressed={scale === option}
                        onClick={() => choose(option)}
                        className={scale === option ? segmentOn : segmentOff}
                    >
                        {interfaceScaleLabel(option)}
                    </button>
                ))}
            </div>
        </section>
    );
}

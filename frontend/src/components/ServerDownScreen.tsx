import { useEffect, useState } from 'react';
import { Copy, Check, Terminal } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { cn } from '@/lib/utils';
import { btn, sectionLabel } from '@/components/ui/formStyles';

const COMMAND = 'uvx collimate';
const POLL_MS = 1500;

type OS = 'macos' | 'windows' | 'linux' | 'unknown';

function detectOS(): OS {
    if (typeof navigator === 'undefined') return 'unknown';
    interface UADataNav { userAgentData?: { platform?: string } }
    const uaData = (navigator as unknown as UADataNav).userAgentData;
    const platform = (uaData?.platform || navigator.platform || '').toLowerCase();
    const ua = (navigator.userAgent || '').toLowerCase();
    if (platform.includes('mac') || ua.includes('mac os')) return 'macos';
    if (platform.includes('win') || ua.includes('windows')) return 'windows';
    if (platform.includes('linux') || ua.includes('linux')) return 'linux';
    return 'unknown';
}

function terminalHint(os: OS): { title: string; steps: string[] } {
    switch (os) {
        case 'macos':
            return {
                title: 'Open Terminal',
                steps: [
                    'Press ⌘ + Space, type "Terminal", hit Return.',
                    'Paste the command and press Return.',
                ],
            };
        case 'windows':
            return {
                title: 'Open PowerShell',
                steps: [
                    'Press Win + X, choose "Terminal" (or "PowerShell").',
                    'Paste the command and press Enter.',
                ],
            };
        case 'linux':
            return {
                title: 'Open Terminal',
                steps: [
                    'Press Ctrl + Alt + T (or launch your terminal app).',
                    'Paste the command and press Enter.',
                ],
            };
        default:
            return {
                title: 'Open a terminal',
                steps: [
                    'Open your terminal app.',
                    'Paste the command and press Enter.',
                ],
            };
    }
}

export function ServerDownScreen({ onOpenSettings }: { onOpenSettings: () => void }) {
    const fetchState = useCollimateStore(s => s.fetchState);
    const [copied, setCopied] = useState(false);
    const [os] = useState<OS>(() => detectOS());
    const hint = terminalHint(os);

    // Poll for the server coming back. fetchState succeeds → store
    // clears apiError → this screen unmounts. fetchState failing is a
    // no-op for us; apiError stays set and the next tick polls again.
    // The next poll waits for the previous one to settle, so a server
    // that accepts connections but hangs doesn't pile up requests.
    useEffect(() => {
        let cancelled = false;
        let timer: number | undefined;
        const tick = async () => {
            await fetchState();
            if (!cancelled) timer = window.setTimeout(tick, POLL_MS);
        };
        timer = window.setTimeout(tick, POLL_MS);
        return () => { cancelled = true; window.clearTimeout(timer); };
    }, [fetchState]);

    const handleCopy = async () => {
        try {
            await navigator.clipboard.writeText(COMMAND);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1800);
        } catch {
            // Clipboard blocked (insecure context, denied permission) —
            // surface a manual-copy fallback by leaving the command
            // visible in the styled card. No UI change needed; the
            // user can select-and-copy from the card text.
        }
    };

    return (
        <div className="flex h-screen w-full items-center justify-center bg-canvas text-ink px-6">
            <div className="w-full max-w-md flex flex-col gap-6">
                <div className="flex flex-col gap-2">
                    <div className="flex items-center gap-2 text-[0.6875rem] font-semibold uppercase tracking-[0.08em] text-red">
                        <Terminal className="w-4 h-4" />
                        <span>Backend offline</span>
                    </div>
                    <h1 className="text-2xl font-semibold leading-tight text-ink">
                        Start the Collimate server
                    </h1>
                    <p className="text-[0.84375rem] text-ink-2">
                        This app talks to a local Python server. Run the command below in
                        a terminal — the page will reconnect on its own.
                    </p>
                </div>

                <div className="flex items-center gap-2 rounded-md border border-line bg-code-bg px-3 py-2.5">
                    <code className="font-mono text-[0.8125rem] text-ink flex-1 select-all">
                        {COMMAND}
                    </code>
                    <button
                        type="button"
                        onClick={handleCopy}
                        className={cn(btn, 'flex-none')}
                        title="Copy command"
                    >
                        {copied ? (
                            <>
                                <Check className="w-3.5 h-3.5 text-green" />
                                <span className="text-green">Copied</span>
                            </>
                        ) : (
                            <>
                                <Copy className="w-3.5 h-3.5" />
                                <span>Copy</span>
                            </>
                        )}
                    </button>
                </div>

                <div className="flex flex-col gap-1.5 text-[0.8125rem] text-ink-2">
                    <div className={sectionLabel}>
                        {hint.title}
                    </div>
                    <ol className="list-decimal list-inside flex flex-col gap-1">
                        {hint.steps.map((step, i) => (
                            <li key={i}>{step}</li>
                        ))}
                    </ol>
                </div>

                <div className="flex items-center gap-3 pt-2">
                    <div className="flex items-center gap-2 text-[0.75rem] text-ink-2">
                        <span className="inline-block w-1.5 h-1.5 rounded-full bg-teal animate-pulse" />
                        <span>Watching for the server…</span>
                    </div>
                    <div className="flex-1" />
                    <button type="button" onClick={onOpenSettings} className={btn}>
                        API settings
                    </button>
                </div>
            </div>
        </div>
    );
}

import React, { useState, useEffect } from 'react';
import { useCollimateFile } from './platform';

// Shared native shadow-DOM media viewer. Every colour, font and radius is a
// skin token (`var(--sheet)`, …): the host writes the tokens on :root and they
// inherit into the shadow root, so no useTheme wiring is needed. The tokens
// are written out here rather than imported from ./index, which re-exports
// this module. Inline styles only: native renderers mount in shadow DOM,
// where className/Tailwind never apply; box-sizing: border-box wherever
// padding is applied since the shadow root ships no CSS reset.

export function MediaViewer({ kind }: { kind: 'audio' | 'video' }) {
    const { fileName, api } = useCollimateFile();
    const url = api.getFileUrl(fileName);
    const [error, setError] = useState<string | null>(null);
    const [retryNonce, setRetryNonce] = useState(0);

    const ext = fileName.split('.').pop()?.toLowerCase() || '';

    // Clear a stale error whenever the target changes (file, cell or host).
    useEffect(() => {
        setError(null);
    }, [url]);

    const handleError = () => {
        setError(`Could not play “${fileName}” — the file may be empty, corrupt, or an unsupported format (.${ext}).`);
    };

    const btnStyle: React.CSSProperties = {
        background: 'none', border: '1px solid var(--line-2)', color: 'var(--ink-2)',
        cursor: 'pointer', padding: '2px 9px', borderRadius: 'calc(var(--radius) - 3px)', fontSize: 12,
        fontFamily: 'var(--font-ui)',
    };

    return (
        <div style={{
            width: '100%', height: '100%', display: 'flex', flexDirection: 'column',
            background: 'var(--sheet)', color: 'var(--ink)', fontFamily: 'var(--font-ui)',
            overflow: 'hidden', boxSizing: 'border-box',
        }}>
            <div style={{
                flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
                overflow: 'auto', boxSizing: 'border-box',
                ...(kind === 'audio' ? { flexDirection: 'column', gap: 18, padding: 24, textAlign: 'center' } : { padding: 16 }),
            }}>
                {error ? (
                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10, maxWidth: 420, ...(kind === 'video' ? { textAlign: 'center' } : {}) }}>
                        <div style={{ color: 'var(--ink)', fontSize: 13, fontWeight: 600 }}>{fileName}</div>
                        <div style={{ color: 'var(--ink-2)', fontSize: 12, lineHeight: 1.5 }}>{error}</div>
                        <button onClick={() => { setError(null); setRetryNonce(n => n + 1); }} style={btnStyle}>Retry</button>
                    </div>
                ) : (
                    <>
                        {kind === 'audio' && <div style={{
                            color: 'var(--ink)', fontSize: 13, fontWeight: 600,
                            wordBreak: 'break-all', maxWidth: '90%',
                        }}>{fileName}</div>}
                        {kind === 'audio' ? <audio
                            key={retryNonce}
                            controls
                            src={url}
                            onError={handleError}
                            style={{ width: 'min(520px, 90%)' }}
                        /> : <video
                            key={retryNonce}
                            controls
                            playsInline
                            src={url}
                            onError={handleError}
                            style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain', outline: 'none' }}
                        />}
                    </>
                )}
            </div>
            <div style={{
                height: 26, background: 'var(--sheet-2)', borderTop: '1px solid var(--line)',
                display: 'flex', alignItems: 'center', padding: '0 12px', gap: 16, boxSizing: 'border-box',
                fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--ink-3)', flexShrink: 0,
                whiteSpace: 'nowrap', overflow: 'hidden',
            }}>
                <span>{fileName}</span>
                <span style={{ marginLeft: 'auto' }}>{ext.toUpperCase()}</span>
            </div>
        </div>
    );
}

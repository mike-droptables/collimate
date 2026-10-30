import React, { useState, useEffect } from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, tokens, useCollimateFile } from '/api/sdk/react.js';

const { color, font } = tokens;

// The transparency checkerboard: sheet-3 squares on a sheet-2 ground, so a
// transparent pixel reads against every skin. Exported for the model test.
export const CHECKERBOARD = Object.freeze({
    backgroundColor: color.sheet2,
    backgroundImage: [
        `linear-gradient(45deg, ${color.sheet3} 25%, transparent 25%)`,
        `linear-gradient(-45deg, ${color.sheet3} 25%, transparent 25%)`,
        `linear-gradient(45deg, transparent 75%, ${color.sheet3} 75%)`,
        `linear-gradient(-45deg, transparent 75%, ${color.sheet3} 75%)`,
    ].join(', '),
    backgroundSize: '20px 20px',
    backgroundPosition: '0 0, 0 10px, 10px -10px, -10px 0px',
});

/** The viewer's box: it fills the renderer's box. */
export const IMAGE_ROOT = Object.freeze({
    width: '100%', height: '100%', display: 'flex', flexDirection: 'column',
    background: color.sheet, color: color.ink, fontFamily: font.ui, overflow: 'hidden',
});

/** The footer: the file's name, size and type, and the zoom controls. */
export const IMAGE_FOOTER = Object.freeze({
    height: 26,
    background: color.sheet2, borderTop: `1px solid ${color.line}`,
    display: 'flex', alignItems: 'center', paddingLeft: 12, paddingRight: 12, gap: 16, boxSizing: 'border-box',
    fontFamily: font.mono, fontSize: 11, color: color.ink3, flexShrink: 0,
    whiteSpace: 'nowrap', overflow: 'hidden',
});

function ImageViewer() {
    const { fileName, api } = useCollimateFile();
    const [imgUrl, setImgUrl] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [naturalSize, setNaturalSize] = useState<{ w: number; h: number } | null>(null);
    const [zoom, setZoom] = useState(1);
    const [retryNonce, setRetryNonce] = useState(0);

    const ext = fileName.split('.').pop()?.toLowerCase() || '';

    useEffect(() => {
        const url = api.getFileUrl(fileName);
        setImgUrl(url);
        setError(null);
        setNaturalSize(null);
        setZoom(1);
    }, [fileName, api, retryNonce]);

    const handleLoad = (e: React.SyntheticEvent<HTMLImageElement>) => {
        const img = e.currentTarget;
        setNaturalSize({ w: img.naturalWidth, h: img.naturalHeight });
    };

    const handleError = () => {
        setError(`Could not display “${fileName}” — the file may be empty, corrupt, or an unsupported format (.${ext}).`);
    };

    const btnStyle: React.CSSProperties = {
        background: 'none', border: `1px solid ${color.line2}`, color: color.ink2,
        cursor: 'pointer', padding: '1px 6px', borderRadius: `calc(${tokens.radius} - 4px)`, fontSize: 11,
        fontFamily: font.mono,
    };

    return (
        <div style={IMAGE_ROOT}>
            <div
                style={{
                    flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
                    overflow: 'auto', padding: 16, boxSizing: 'border-box',
                    ...CHECKERBOARD,
                }}
            >
                {error ? (
                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10, textAlign: 'center', maxWidth: 420 }}>
                        <div style={{ color: color.ink, fontSize: 13, fontWeight: 600 }}>{fileName}</div>
                        <div style={{ color: color.ink2, fontSize: 12, lineHeight: 1.5 }}>{error}</div>
                        <button onClick={() => setRetryNonce(n => n + 1)} style={btnStyle}>Retry</button>
                    </div>
                ) : imgUrl ? (
                    <img
                        key={retryNonce}
                        src={imgUrl}
                        onLoad={handleLoad}
                        onError={handleError}
                        style={{
                            maxWidth: zoom === 1 ? '100%' : 'none',
                            maxHeight: zoom === 1 ? '100%' : 'none',
                            width: zoom !== 1 ? `${zoom * 100}%` : 'auto',
                            height: 'auto',
                            objectFit: 'contain',
                            imageRendering: zoom > 2 ? 'pixelated' : 'auto',
                        }}
                        draggable={false}
                    />
                ) : (
                    <div style={{ color: color.ink3, fontFamily: font.mono, fontSize: 12 }}>loading…</div>
                )}
            </div>
            <div style={IMAGE_FOOTER}>
                <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>{fileName}</span>
                {naturalSize && <span style={{ flexShrink: 0 }}>{naturalSize.w} x {naturalSize.h}</span>}
                <span style={{ flexShrink: 0 }}>{ext.toUpperCase()}</span>
                <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
                    <button onClick={() => setZoom(z => Math.max(0.25, z / 2))} style={btnStyle}>-</button>
                    <span>{Math.round(zoom * 100)}%</span>
                    <button onClick={() => setZoom(z => Math.min(8, z * 2))} style={btnStyle}>+</button>
                    <button onClick={() => setZoom(1)} style={btnStyle}>Fit</button>
                </div>
            </div>
        </div>
    );
}

export default defineRenderer({ component: ImageViewer });

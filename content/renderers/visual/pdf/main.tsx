// SPDX-License-Identifier: MIT
//
// PDF renderer: EmbedPDF's headless React components over its PDFium
// WebAssembly engine, in the native shadow DOM.
//
// Section map:
//   §1  Imports + esm.sh dependency pinning (read the DEPS note)
//   §2  The engine: PDFium on the main thread, its pinned wasm through the
//       app's module mirror
//   §3  Styles on the skin tokens, the pointer modes, and the pure labels
//       the view controls show
//   §4  The viewer: engine → document → pages, and the view controls in the
//       SDK's corner StyleControl (no bar of its own)
//
// §1 DEPS — every @embedpdf/* import below carries the byte-identical deps
// list `react@19.3.1,react-dom@19.3.1`. esm.sh builds a package once per
// deps list, under a path that encodes it, and every plugin's React module
// imports @embedpdf/core at THAT path. The one list makes the plugins, the
// provider and the `createPluginRegistration` they are registered with share
// one core module instance; a core imported with another list (or none) is a
// second instance whose registrations the provider's registry refuses. The
// list also lands every React import on the build `/api/sdk/react.js` uses.
// The SDK's module snapshot freezes what the list resolves
// (module-snapshot/, which the app serves before upstream).
// ---------------------------------------------------------------------------
import React, { useEffect, useMemo, useState } from 'https://esm.sh/react@19.3.1?target=es2022';
import { createPluginRegistration } from 'https://esm.sh/@embedpdf/core@2.15.1?deps=react@19.3.1,react-dom@19.3.1&target=es2022';
import { EmbedPDF } from 'https://esm.sh/@embedpdf/core@2.15.1/react?deps=react@19.3.1,react-dom@19.3.1&target=es2022';
import { usePdfiumEngine } from 'https://esm.sh/@embedpdf/engines@2.15.1/react?deps=react@19.3.1,react-dom@19.3.1&target=es2022';
import { DocumentContent, DocumentManagerPluginPackage, useActiveDocument } from 'https://esm.sh/@embedpdf/plugin-document-manager@2.15.1/react?deps=react@19.3.1,react-dom@19.3.1&target=es2022';
import { Viewport, ViewportPluginPackage } from 'https://esm.sh/@embedpdf/plugin-viewport@2.15.1/react?deps=react@19.3.1,react-dom@19.3.1&target=es2022';
import { Scroller, ScrollPluginPackage, useScroll } from 'https://esm.sh/@embedpdf/plugin-scroll@2.15.1/react?deps=react@19.3.1,react-dom@19.3.1&target=es2022';
import { RenderLayer, RenderPluginPackage } from 'https://esm.sh/@embedpdf/plugin-render@2.15.1/react?deps=react@19.3.1,react-dom@19.3.1&target=es2022';
import { ZoomGestureWrapper, ZoomMode, ZoomPluginPackage, useZoom } from 'https://esm.sh/@embedpdf/plugin-zoom@2.15.1/react?deps=react@19.3.1,react-dom@19.3.1&target=es2022';
import { GlobalPointerProvider, InteractionManagerPluginPackage, PagePointerProvider, useInteractionManagerCapability } from 'https://esm.sh/@embedpdf/plugin-interaction-manager@2.15.1/react?deps=react@19.3.1,react-dom@19.3.1&target=es2022';
import { PanPluginPackage } from 'https://esm.sh/@embedpdf/plugin-pan@2.15.1/react?deps=react@19.3.1,react-dom@19.3.1&target=es2022';
import { SelectionLayer, SelectionPluginPackage } from 'https://esm.sh/@embedpdf/plugin-selection@2.15.1/react?deps=react@19.3.1,react-dom@19.3.1&target=es2022';
import { defineRenderer, mirrorUrl, tokens, useCollimateFile, StyleControl, StyleControlRow } from '/api/sdk/react.js';

const { color, font } = tokens;

// ---------------------------------------------------------------------------
// §2 The engine
// ---------------------------------------------------------------------------
// One EmbedPDF release for every package above, and for the PDFium build the
// engine downloads. The build is the one `@embedpdf/engines` names as its
// default; it is pinned here so the renderer never follows a CDN redirect,
// and the node test holds the two pins together (theme.test.ts). The engine
// fetches it, so no import map routes it: mirrorUrl gives the app's
// same-origin copy (the module snapshot freezes this literal).
export const PDFIUM_WASM_URL = 'https://cdn.jsdelivr.net/npm/@embedpdf/pdfium@2.15.1/dist/pdfium.wasm';

// The engine runs on this document's thread, not in a worker. PDFium renders
// a page in a few milliseconds; the frame it takes is spent once per page, as
// it scrolls into view.
export const ENGINE_OPTIONS = Object.freeze({ wasmUrl: mirrorUrl(PDFIUM_WASM_URL), worker: false });

// ---------------------------------------------------------------------------
// §3 Styles and labels
// ---------------------------------------------------------------------------
// Inline styles on the skin tokens (they inherit into the shadow root). The
// pages are the one thing not on the skin: a page is the document as
// authored, and PDFium paints it onto its own white, so the page box's ground
// is the light scheme's Canvas, as the document view gives an authored page.

/** The viewer's box: it fills the renderer's box. Positioned, so the corner
 *  control anchors to it; there is no other chrome. */
export const PDF_ROOT = Object.freeze({
    position: 'relative', width: '100%', height: '100%', display: 'flex', flexDirection: 'column',
    background: color.sheet, color: color.ink, fontFamily: font.ui, fontSize: 12, overflow: 'hidden',
});

/** The row the pages scroll in: all of the box. */
export const PDF_BODY = Object.freeze({ flex: 1, minHeight: 0, position: 'relative', display: 'flex', flexDirection: 'column' });

/** EmbedPDF's viewport, the scroll container: it fills the body, on the
 *  raised sheet the pages lift from. Border-box, because the viewport pads
 *  itself by its gap: the fit zooms size the pages to its client box less
 *  that gap, so the padding has to sit inside the body's width, not past it
 *  (content-box would push the box, its right margin and its scrollbar 2×gap
 *  past the clipped edge). */
export const PDF_VIEWPORT = Object.freeze({ width: '100%', height: '100%', boxSizing: 'border-box', background: color.sheet2 });

/** The pointer layer inside the viewport (EmbedPDF's global pointer
 *  provider, where the pan mode listens): at least the viewport's size and
 *  as large as the pages, so a press anywhere over the scrolled area, page
 *  or margin, drags the document, while the viewport's own scrollbars stay
 *  outside it. */
export const PDF_POINTER_LAYER = Object.freeze({ width: 'fit-content', minWidth: '100%', height: 'auto', minHeight: '100%' });

/** One page's box, sized by the scroller; the render layer and the selection
 *  layer fill it. Never selected or dragged by the browser: a press drags
 *  the document (Pan) or selects text through the selection layer (Select),
 *  never picks up the page's image. */
export const PDF_PAGE = Object.freeze({
    position: 'relative', background: 'Canvas', colorScheme: 'light', boxShadow: tokens.shadow,
    userSelect: 'none', WebkitUserSelect: 'none',
});

/** The view controls' buttons, in the corner control's popover: the look
 *  of its own steps. */
export const PDF_BUTTON = Object.freeze({
    height: 24, minWidth: 28, padding: '0 6px', margin: 0, boxSizing: 'border-box',
    border: `1px solid ${color.line2}`, borderRadius: `calc(${tokens.radius} - 2px)`,
    background: color.sheet2, color: color.ink, font: 'inherit', cursor: 'pointer',
});

/** A button that cannot act now (the first page's Previous). */
export const PDF_BUTTON_OFF = Object.freeze({ color: color.ink3, cursor: 'default' });

/** The chosen half of a two-way segment (Pan | Select): the selection
 *  accent's soft fill. */
export const PDF_BUTTON_ON = Object.freeze({ background: color.brassSoft });

/** The corners of a two-way segment's halves: the outer ones rounded. */
export const PDF_SEGMENT_RADIUS = Object.freeze([
    `calc(${tokens.radius} - 2px) 0 0 calc(${tokens.radius} - 2px)`,
    `0 calc(${tokens.radius} - 2px) calc(${tokens.radius} - 2px) 0`,
]);

/** A readout between two buttons (the page, the zoom). */
export const PDF_READOUT = Object.freeze({ minWidth: 48, textAlign: 'center', fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' });

export const PDF_LINK = Object.freeze({
    color: color.ink2, textDecoration: 'underline', textDecorationColor: color.line2,
    textUnderlineOffset: 3, font: 'inherit',
});

/** Selected text: the selection accent, as a fill over the page. */
export const SELECTION_FILL = 'color-mix(in srgb, var(--brass) 35%, transparent)';

// What a press over the pages does. EmbedPDF's interaction manager runs one
// mode at a time per document: the pan plugin's `panMode` (a press and drag
// scrolls the document; the mode is global, so it listens on the pointer
// layer) or the manager's own `pointerMode` (page-scoped; the selection
// layer selects text in it). Pan is the default: the pan plugin makes it the
// manager's default mode for every document it opens (`PAN_CONFIG`).

export type PointerChoice = 'pan' | 'select';

/** The interaction mode behind each choice. */
export const POINTER_MODES: Readonly<Record<PointerChoice, string>> = Object.freeze({ pan: 'panMode', select: 'pointerMode' });

/** The Pointer row's segment, Pan first (the default). */
export const POINTER_CHOICES: ReadonlyArray<Readonly<{ id: PointerChoice; label: string; title: string }>> = Object.freeze([
    Object.freeze({ id: 'pan' as const, label: 'Pan', title: 'Drag to move through the document' }),
    Object.freeze({ id: 'select' as const, label: 'Select', title: 'Drag to select text' }),
]);

/** The pan plugin's configuration: pan is every document's default mode. */
export const PAN_CONFIG = Object.freeze({ defaultMode: 'always' as const });

/** The selection plugin's configuration: Select selects text, nothing else.
 *  Its marquee (a drag over a page's blank area drawing a box to capture
 *  that region) is off. */
export const SELECTION_CONFIG = Object.freeze({ marquee: Object.freeze({ enabled: false }) });

/** The choice the manager's active mode shows: Select only in the select
 *  mode; any other mode (pan, or none yet) is Pan, the default. */
export function pointerChoiceOf(activeMode: unknown): PointerChoice {
    return activeMode === POINTER_MODES.select ? 'select' : 'pan';
}

/** The mode a choice activates; anything but 'select' is Pan. */
export function interactionModeFor(choice: unknown): string {
    return choice === 'select' ? POINTER_MODES.select : POINTER_MODES.pan;
}

/** The zoom readout for a zoom factor. */
export function zoomLabel(level: unknown): string {
    return typeof level === 'number' && Number.isFinite(level) && level > 0 ? `${Math.round(level * 100)}%` : '—';
}

/** The page readout: 1-based current page of the total. */
export function pageLabel(current: unknown, total: unknown): string {
    if (typeof total !== 'number' || !(total > 0)) return '— / —';
    const page = typeof current === 'number' && current > 0 ? Math.min(Math.round(current), total) : 1;
    return `${page} / ${total}`;
}

/** What the viewer says when the document does not open. */
export function documentFailureMessage(fileName: string): string {
    return `Could not open “${fileName}” — the file may be empty, corrupt, or not a PDF.`;
}

/** What the viewer says when the engine does not start. */
export function engineFailureMessage(error: unknown): string {
    const detail = error instanceof Error ? error.message : typeof error === 'string' ? error : '';
    return `The PDF engine could not start${detail ? ` (${detail})` : ''}. It loads once through the app's module mirror; check the connection and retry.`;
}

// ---------------------------------------------------------------------------
// §4 The viewer
// ---------------------------------------------------------------------------

function Notice({ children }: { children: React.ReactNode }) {
    return (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: color.ink3, fontFamily: font.mono, fontSize: 12, background: color.sheet2 }}>
            {children}
        </div>
    );
}

function Failure({ fileName, url, message, retry }: { fileName: string; url: string; message: string; retry: () => void }) {
    return (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24, boxSizing: 'border-box', background: color.sheet2 }}>
            <div role="alert" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10, textAlign: 'center', maxWidth: 420 }}>
                <div style={{ color: color.ink, fontSize: 13, fontWeight: 600 }}>{fileName}</div>
                <div style={{ color: color.ink2, fontSize: 12, lineHeight: 1.5 }}>{message}</div>
                <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
                    <button type="button" onClick={retry} style={PDF_BUTTON}>Retry</button>
                    <a href={url} target="_blank" rel="noopener noreferrer" style={PDF_LINK}>Open in new tab ↗</a>
                </div>
            </div>
        </div>
    );
}

/** The box around whatever the body shows; once a document is open the
 *  corner control carries the view controls. */
function Frame({ controls, children }: { controls?: React.ReactNode; children: React.ReactNode }) {
    return (
        <div style={PDF_ROOT}>
            <div style={PDF_BODY}>{children}</div>
            {controls}
        </div>
    );
}

/** One of the view rows' buttons; its label is its title. */
function Step({ title, label, disabled, onClick, children }: { title: string; label?: string; disabled?: boolean; onClick: () => void; children: React.ReactNode }) {
    return (
        <button type="button" onClick={onClick} disabled={disabled} title={title} aria-label={label ?? title}
            style={{ ...PDF_BUTTON, ...(disabled ? PDF_BUTTON_OFF : null) }}>
            {children}
        </button>
    );
}

/** The interaction manager's active mode for the document, as a pointer
 *  choice, and the setter that activates a choice's mode. */
function usePointerChoice(documentId: string): [PointerChoice, (choice: PointerChoice) => void] {
    const { provides } = useInteractionManagerCapability();
    const [active, setActive] = useState<string | null>(() => (provides ? provides.forDocument(documentId).getActiveMode() : null));
    useEffect(() => {
        if (!provides) return undefined;
        const scope = provides.forDocument(documentId);
        setActive(scope.getActiveMode());
        return scope.onModeChange((mode: string) => setActive(mode));
    }, [provides, documentId]);
    const choose = (choice: PointerChoice) => {
        if (provides) provides.forDocument(documentId).activate(interactionModeFor(choice));
    };
    return [pointerChoiceOf(active), choose];
}

/** The view controls, rows of the corner control's popover: page, zoom,
 *  fit, pointer mode and the file in a new tab. Read from the open
 *  document's scroll, zoom and interaction state; inside the EmbedPDF
 *  provider (the popover renders in its tree). */
function ViewRows({ documentId }: { documentId: string }) {
    const { state: scroll, provides: scroller } = useScroll(documentId);
    const { state: zoom, provides: zoomer } = useZoom(documentId);
    const [pointer, choosePointer] = usePointerChoice(documentId);
    return (
        <>
            <StyleControlRow label="Page">
                <Step title="Previous page" disabled={!scroller || scroll.currentPage <= 1} onClick={() => scroller?.scrollToPreviousPage()}>‹</Step>
                <span style={PDF_READOUT}>{pageLabel(scroll.currentPage, scroll.totalPages)}</span>
                <Step title="Next page" disabled={!scroller || scroll.currentPage >= scroll.totalPages} onClick={() => scroller?.scrollToNextPage()}>›</Step>
            </StyleControlRow>
            <StyleControlRow label="Zoom">
                <Step title="Zoom out" disabled={!zoomer} onClick={() => zoomer?.zoomOut()}>−</Step>
                <span style={PDF_READOUT}>{zoomLabel(zoom.currentZoomLevel)}</span>
                <Step title="Zoom in" disabled={!zoomer} onClick={() => zoomer?.zoomIn()}>+</Step>
            </StyleControlRow>
            <StyleControlRow label="Fit">
                <Step title="Fit the page width" label="Fit width" disabled={!zoomer} onClick={() => zoomer?.requestZoom(ZoomMode.FitWidth)}>Width</Step>
                <Step title="Fit a whole page" label="Fit page" disabled={!zoomer} onClick={() => zoomer?.requestZoom(ZoomMode.FitPage)}>Page</Step>
            </StyleControlRow>
            <StyleControlRow label="Pointer">
                <div role="group" aria-label="Pointer" style={{ display: 'flex' }}>
                    {POINTER_CHOICES.map((choice, i) => (
                        <button key={choice.id} type="button" aria-pressed={pointer === choice.id} title={choice.title}
                            onClick={() => choosePointer(choice.id)}
                            style={{
                                ...PDF_BUTTON, ...(pointer === choice.id ? PDF_BUTTON_ON : null),
                                borderRadius: PDF_SEGMENT_RADIUS[i], marginLeft: i === 0 ? 0 : -1,
                            }}>
                            {choice.label}
                        </button>
                    ))}
                </div>
            </StyleControlRow>
        </>
    );
}

/** The corner control: no style of its own to offer (the pages are the
 *  document as authored), only the view rows. */
function ViewControl({ documentId }: { documentId: string }) {
    const { api } = useCollimateFile();
    return <StyleControl api={api} themes={[]} font={false} size={false} extra={<ViewRows documentId={documentId} />} />;
}

/** The pages: EmbedPDF's viewport scrolls them, the scroller lays them out,
 *  each rendered as an image by the engine with the selection layer over it.
 *  The pointer layer under the viewport is where the pan mode hears a press
 *  and drag; the page image is never draggable. Ctrl/⌘ + wheel and pinch
 *  zoom through the gesture wrapper. */
function Pages({ documentId }: { documentId: string }) {
    return (
        <Viewport documentId={documentId} style={PDF_VIEWPORT}>
            <GlobalPointerProvider documentId={documentId} style={PDF_POINTER_LAYER}>
                <ZoomGestureWrapper documentId={documentId}>
                    <Scroller
                        documentId={documentId}
                        renderPage={({ pageIndex, width, height }: { pageIndex: number; width: number; height: number }) => (
                            <div style={{ ...PDF_PAGE, width, height }} onDragStart={(e: React.DragEvent) => e.preventDefault()}>
                                <PagePointerProvider documentId={documentId} pageIndex={pageIndex}>
                                    <RenderLayer documentId={documentId} pageIndex={pageIndex} draggable={false} />
                                    <SelectionLayer documentId={documentId} pageIndex={pageIndex} background={SELECTION_FILL} />
                                </PagePointerProvider>
                            </div>
                        )}
                    />
                </ZoomGestureWrapper>
            </GlobalPointerProvider>
        </Viewport>
    );
}

/** The document the manager opened from the file's URL, through its loading
 *  and failure states; inside the EmbedPDF provider. */
function Document({ fileName, url, retry }: { fileName: string; url: string; retry: () => void }) {
    const { activeDocumentId } = useActiveDocument();
    if (!activeDocumentId) return <Frame><Notice>Opening {fileName}…</Notice></Frame>;
    return (
        <DocumentContent documentId={activeDocumentId}>
            {({ isLoading, isError, isLoaded }: { isLoading: boolean; isError: boolean; isLoaded: boolean }) => (
                <Frame controls={isLoaded ? <ViewControl documentId={activeDocumentId} /> : null}>
                    {isLoading && <Notice>Opening {fileName}…</Notice>}
                    {isError && <Failure fileName={fileName} url={url} message={documentFailureMessage(fileName)} retry={retry} />}
                    {isLoaded && <Pages documentId={activeDocumentId} />}
                </Frame>
            )}
        </DocumentContent>
    );
}

/** One engine and one document for the mounted file; Retry remounts it. */
function PdfSession({ fileName, url, retry }: { fileName: string; url: string; retry: () => void }) {
    const { engine, isLoading, error } = usePdfiumEngine(ENGINE_OPTIONS);
    const plugins = useMemo(() => [
        createPluginRegistration(DocumentManagerPluginPackage, { initialDocuments: [{ url }] }),
        createPluginRegistration(ViewportPluginPackage),
        createPluginRegistration(ScrollPluginPackage),
        createPluginRegistration(RenderPluginPackage),
        createPluginRegistration(InteractionManagerPluginPackage),
        createPluginRegistration(PanPluginPackage, PAN_CONFIG),
        createPluginRegistration(ZoomPluginPackage, { defaultZoomLevel: ZoomMode.FitWidth }),
        createPluginRegistration(SelectionPluginPackage, SELECTION_CONFIG),
    ], [url]);
    if (error) return <Frame><Failure fileName={fileName} url={url} message={engineFailureMessage(error)} retry={retry} /></Frame>;
    if (isLoading || !engine) return <Frame><Notice>Loading the PDF engine…</Notice></Frame>;
    return (
        <EmbedPDF engine={engine} plugins={plugins}>
            <Document fileName={fileName} url={url} retry={retry} />
        </EmbedPDF>
    );
}

function PdfViewer() {
    const { fileName, api } = useCollimateFile();
    const [attempt, setAttempt] = useState(0);
    // The file's bytes, binary-safe and pinned to the accepted revision; a
    // write to the file remounts the renderer with the new URL.
    const url = api.getFileUrl(fileName);
    return <PdfSession key={`${url}#${attempt}`} fileName={fileName} url={url} retry={() => setAttempt(n => n + 1)} />;
}

export default defineRenderer({ component: PdfViewer });

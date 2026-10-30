// SPDX-License-Identifier: MIT
//
// Types for the platform-served renderer SDK. Renderer .tsx files
// import from the virtual URL `/api/sdk/react.js`, which only resolves
// inside the Collimate sandbox; mapping that specifier here lets the
// same files typecheck under plain `tsc`.
//
// TypeScript forbids ambient `declare module` for rooted paths
// (TS2436), so this is a normal declaration module — wire it up with a
// `paths` entry in your tsconfig:
//
//   "compilerOptions": {
//     "paths": {
//       "/api/sdk/react.js": ["./node_modules/@collimate/renderer-hooks/platform"]
//     }
//   }

import type {
    AnnotationApi,
    AnnotationHandler,
    CellDropApi,
    CollimateFile,
    DefineRendererConfig,
    DesktopApi,
    DesktopSurface,
    EditingApi,
    EditingHandle,
    HostFocusApi,
    HostFocusHandle,
    RemoteModuleState,
    RemoteScriptState,
    RendererDefinition,
    RendererTheme,
    SiblingFileState,
    StyleApi,
    StyleHandle,
    SyntaxPalette,
    SyntaxRole,
    ThemeApi,
    ThemeCssOptions,
    ThemeHandle,
    ThemeVarName,
    VisibilityApi,
    VisibilityHandle,
} from '@collimate/renderer-hooks';

export function useCollimateFile(): CollimateFile;
export function useSiblingFile(path: string): SiblingFileState;
export function defineRenderer(config: DefineRendererConfig): RendererDefinition;

// Same implementations this package ships, re-served by the host.
export function useTheme(api: ThemeApi | null | undefined): ThemeHandle;
export function useEditing(api: EditingApi | null | undefined): EditingHandle;
export function useStyle(api: StyleApi | null | undefined): StyleHandle;
/** The shared style icon (top right, shown only while the pointer is near its corner) and its popover: theme,
 *  font and text size, written through `api.setStyle`, and the renderer's own rows under them (`extra`).
 *  Inline styles only. */
export const StyleControl: (typeof import('@collimate/renderer-hooks'))['StyleControl'];
/** One row of a `StyleControl`'s `extra`: a label and the controls beside it, styled like the style rows. */
export const StyleControlRow: (typeof import('@collimate/renderer-hooks'))['StyleControlRow'];
/** The drag type the files pane writes `{cellId, paths}` under. */
export const CELL_FILES_MIME: string;
/** Whether a drag carries files from the files pane (types only: works in
 *  `dragover`). */
export function isCellFileDrag(dt: DataTransfer | null | undefined): boolean;
/** The dropped cell-relative paths when they are this renderer's cell's;
 *  null otherwise. */
export function readCellFileDrop(dt: DataTransfer | null | undefined, api: CellDropApi | null | undefined): string[] | null;
export function themeCss(theme: RendererTheme, opts?: ThemeCssOptions): string;
export function resolveVar(theme: RendererTheme, name: ThemeVarName): string;
export function toHex8(cssColor: string): string | null;
export function syntaxPalette(theme: RendererTheme): SyntaxPalette | null;
export function useDesktop(api: DesktopApi | null | undefined): DesktopSurface | undefined;
export function useVisibility(api: VisibilityApi | null | undefined): VisibilityHandle;
export function useHostFocus(api: HostFocusApi | null | undefined): HostFocusHandle;
/** Answer the host's annotation requests (`api.onAnnotationRequest`): subscribes once per `api`, always calling
 *  the latest `handler`; null withdraws it. Inert where the host has no `onAnnotationRequest`. */
export function useAnnotationRequest(api: AnnotationApi | null | undefined, handler: AnnotationHandler | null): void;
/** The rejection that tells the host this view cannot answer an annotation request; the host shows `message`. */
export function annotationUnavailable(message: string): Error;
export function useRemoteModule<T = unknown>(url: string): RemoteModuleState<T>;
export function useRemoteScript(url: string): RemoteScriptState;
/** The app's same-origin mirror of an `https://esm.sh/` or `https://cdn.jsdelivr.net/` URL, for a load an
 *  import map does not cover (a fetched stylesheet, a font, wasm, a `<script>`, a worker); any other URL,
 *  and every URL outside an app host (a sealed preview), unchanged. ES module imports need no call. */
export function mirrorUrl(url: string): string;

export const tokens: (typeof import('@collimate/renderer-hooks'))['tokens'];
export const THEME_VAR_NAMES: readonly ThemeVarName[];
export const SYNTAX_ROLES: readonly SyntaxRole[];

export const MediaViewer: (typeof import('@collimate/renderer-hooks'))['MediaViewer'];

// The document view: an authored HTML document in a renderer's own opaque
// frame (DarkReader on the skin, the backing, the dark choice the caller makes).
type DocumentViewModule = typeof import('@collimate/renderer-hooks');
export type {
    AnnotationRequest, AnnotationContext, AnnotationHandler, ElementPick,
    DarkReaderOptions, DarkReaderInjection, PreparedDocument, DocumentViewInput, DocumentView,
} from '@collimate/renderer-hooks';
export const DARKREADER_VERSION: DocumentViewModule['DARKREADER_VERSION'];
export const DARKREADER_PATH: DocumentViewModule['DARKREADER_PATH'];
export const DARKREADER_MESSAGE: DocumentViewModule['DARKREADER_MESSAGE'];
export const HOST_THEME_WAIT_MS: DocumentViewModule['HOST_THEME_WAIT_MS'];
export const DOCUMENT_VIEW_STYLE: DocumentViewModule['DOCUMENT_VIEW_STYLE'];
export const darkReaderScriptUrl: DocumentViewModule['darkReaderScriptUrl'];
export const darkReaderPoles: DocumentViewModule['darkReaderPoles'];
export const darkReaderOptions: DocumentViewModule['darkReaderOptions'];
export const injectDarkReader: DocumentViewModule['injectDarkReader'];
export const documentSrcDoc: DocumentViewModule['documentSrcDoc'];
export const hostThemeKnown: DocumentViewModule['hostThemeKnown'];
export const themeSettled: DocumentViewModule['themeSettled'];
export const documentSkinKey: DocumentViewModule['documentSkinKey'];
export const documentSkinPost: DocumentViewModule['documentSkinPost'];
export const deliveredAfterLoad: DocumentViewModule['deliveredAfterLoad'];
export const frameBacking: DocumentViewModule['frameBacking'];
export const useDocumentView: DocumentViewModule['useDocumentView'];

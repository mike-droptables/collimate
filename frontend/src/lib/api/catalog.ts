import { requestJson } from './client';


/** Full renderer list incl. disabled ones, each stamped with `available` +
 *  `cell_id`: the Capabilities manage view's renderer rows. */
export const fetchRenderersWithCell = async (): Promise<RendererEntry[]> =>
    requestJson('/renderers?include_disabled=1', 'GET');

/** One generator on the active channel's Capabilities stack, as GET
 *  /generators reads it from its manifest: the Capabilities manage view's
 *  generator rows. `name` is the manifest id, `display_name` its name. */
export interface GeneratorEntry {
    name: string;
    display_name: string;
    summary: string;
    cell_id: string;
    /** The name and icon of the Capabilities cell that holds it. */
    group: string;
    group_icon: string | null;
    /** A long-lived process (a daemon) rather than a one-shot run. */
    daemon: boolean;
    /** A chat-capable run. */
    chat: boolean;
    /** The only config folders a runner may provide to this generator. */
    config_folders: { name: string; description?: string }[];
    /** Mid-run file editing: "none" locks the files during the run. */
    live_files_mode: string;
    /** The manifest's version and entrypoint, null when it declares none. */
    version: string | null;
    entrypoint: string | null;
    /** The run's interface file suffixes. */
    ui_files: string[];
    requires_docker: boolean;
}

export const fetchGenerators = async (): Promise<GeneratorEntry[]> => requestJson('/generators', 'GET');

export const fetchRendererPreferences = async (): Promise<Record<string, string>> => requestJson('/renderer_preferences', 'GET');

export const saveRendererPreferences = async (prefs: Record<string, string>) => requestJson('/renderer_preferences', 'PUT', prefs);

/** Window event dispatched after any renderer prefs/availability/display
 *  or handler mutation; the renderer resolver cache and the Capabilities
 *  manage view reload on it. */
export const RENDERER_PREFS_CHANGED_EVENT = 'collimate:renderer-prefs-changed';


// --- Renderer availability (the Capabilities manage view's renderer rows) ---
export interface RendererEntry {
    id: string;
    name: string;
    version: string;
    summary: string;
    extensions: string[];
    /** The files it is a default for: extensions it claims, or `*` (every
     *  text file's "Open with" menu offers it). */
    default_for: string[];
    supports_diff: boolean;
    embed: string;
    content_hash: string;
    cell_id?: string;
    available?: boolean;
    /** One of the app's built-in renderers (listed after the user's): always
     *  available, its package read-only. */
    builtin?: boolean;
}

export const setRendererAvailability = async (id: string, enabled: boolean) =>
    requestJson(`/renderer_availability/${encodeURIComponent(id)}`, 'PUT', { enabled });

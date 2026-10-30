import type { UserTheme } from '@/lib/userThemes';
import type { PersistedCommandPane } from '@/stores/collimate/commandPaneSlice';
import { requestJson } from './client';


// ── Per-channel settings (the channel settings document) ──
// One doc per channel, three sections each owned by one frontend store —
// keyboard shortcut bindings, skins (active id + user skin library) and the
// editing preferences — plus the layout prefs, the overview's remove zones,
// each renderer's style choices (useRendererStylesStore) and the renderer
// sections the server composes from the channel config. PUT is section-wholesale: send
// only the section(s) you own. The settings view's JSON import sends every
// section of an exported doc at once.

export interface ChannelSettingsEditing {
    spellcheck: boolean;
}

/** One renderer's style choices, each null for the renderer's default (the
 *  skin's look, the role's skin font, the renderer's own text size). `font`
 *  is a font catalogue family name; `textSize` a factor. */
export interface RendererStyleRecord {
    theme: string | null;
    font: string | null;
    textSize: number | null;
}

/** Each renderer's style choices by renderer id. */
export type ChannelSettingsRendererStyles = Record<string, RendererStyleRecord>;

/** What a remove zone does to a board card let go over it: a plain remove,
 *  or a remove that adds or takes away one tag on the removed record. */
export type ZoneAction =
    | { kind: 'remove' }
    | { kind: 'add_tag'; tag: string }
    | { kind: 'remove_tag'; tag: string };

/** One stretch of the Removed bar and the shelf's drop area: its share of
 *  the width (the zones' widths sum to 1) and its action. */
export interface RemoveZone {
    id: string;
    width: number;
    action: ZoneAction;
}

export interface ChannelSettings {
    version: number;
    kind: 'channel-settings';
    /** Shortcut bindings (useKeybindingsStore): `overrides` rebinds registry
     *  shortcuts by id, `commands` binds Commands pane rows by command key;
     *  both map to canonical combo strings. */
    keyboard: { overrides: Record<string, string>; commands: Record<string, string> };
    theme: { activeThemeId: string | null; userThemes: UserTheme[] };
    /** Editing preferences every editable follows (useEditingStore):
     *  `spellcheck` underlines misspelled words in the app's fields and in
     *  every renderer's editors. */
    editing: ChannelSettingsEditing;
    /** Canvas-layout preferences. `defaultStackWidth` is what the stack
     *  width control's Reset returns to (px; null = the built-in 500);
     *  `commandPane` is the Commands pane's state (open, dock side, query,
     *  recent commands); `removedShelfHeight` is the overview's Removed
     *  shelf open height (px; null = the built-in 316); `overviewShowTags`
     *  is the overview's tag toggle (absent = off). PUT replaces the whole
     *  section: a writer sends the snapshot's other keys with its own. */
    layout?: {
        defaultStackWidth: number | null;
        commandPane?: PersistedCommandPane;
        removedShelfHeight?: number | null;
        overviewShowTags?: boolean;
    };
    /** The remove zones, left to right (at most 6). An empty list is one
     *  full-width plain remove. The server drops invalid zones, normalises
     *  their tags and renormalises the widths. */
    remove_zones: { zones: RemoveZone[] };
    /** Each renderer's style choices (theme, font, text size) by renderer
     *  id, set from its style control. The server drops bad ids and values
     *  and every all-null entry. PUT replaces the whole map. */
    renderer_styles?: ChannelSettingsRendererStyles;
    /** Which renderers the channel enables, by renderer id. */
    renderer_availability?: Record<string, boolean>;
    /** The renderer each file extension opens in, by extension. */
    renderer_preferences?: Record<string, string>;
}

export const fetchChannelSettings = async (channel: string): Promise<ChannelSettings> =>
    requestJson(`/channels/${encodeURIComponent(channel)}/settings`, 'GET');

export const putChannelSettings = async (
    channel: string, partial: Partial<ChannelSettings>,
): Promise<ChannelSettings> =>
    requestJson(`/channels/${encodeURIComponent(channel)}/settings`, 'PUT', partial);

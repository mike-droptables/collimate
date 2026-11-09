import type { PersistedCommandPane } from '@/stores/collimate/commandPaneSlice';
import { requestJson } from './client';
import { isReadOnlyChannel } from '../channelArchived';


// ── Per-channel settings (the channel settings document) ──
// One doc per channel: keyboard shortcut bindings, the channel's theme
// override (the themes themselves and the default are the server's,
// ./appSettings), the layout prefs, the overview's remove zones, each
// renderer's style choices (useRendererStylesStore) and the renderer
// sections the server composes from the channel config. PUT is
// section-wholesale: send only the section(s) you own.

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
    /** The channel's theme override: a theme id (a built-in, or a user
     *  theme in the server's library), or null to follow the server's
     *  default theme. */
    theme: { override: string | null };
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

/** Save sections of a channel's settings document. An archived channel
 *  takes no write (the server would refuse it): the change stays in this
 *  window, and the promise rejects with ChannelReadOnlyError, which the
 *  view-state savers swallow like any failed save. */
export const putChannelSettings = async (
    channel: string, partial: Partial<ChannelSettings>,
): Promise<ChannelSettings> => {
    if (isReadOnlyChannel(channel)) throw new ChannelReadOnlyError();
    return requestJson(`/channels/${encodeURIComponent(channel)}/settings`, 'PUT', partial);
};

/** A settings write skipped because the channel is archived. */
export class ChannelReadOnlyError extends Error {
    constructor() { super('This channel is archived and read-only. Unarchive it to change it.'); }
}

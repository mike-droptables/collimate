import type { HandlerInfo } from './definitionApi';

/** What a handler is and what its status means, shown once at the top of
 *  the Capabilities manage view's Handlers group. */
export const HANDLER_MODEL =
    'A handler teaches agents a file type and checks their files. Its checks and skills come from a build: Build opens a work stack, you add its result back, and nothing it makes is used until you review and accept it here.';

/** Where a handler stands. A user handler: `off` (another handler has its
 *  id), `none` (never built), `review-first` (a first build waits),
 *  `in-use`, `review-next` (a newer build waits beside the one in use) and
 *  `orphaned` (the accepted build is in use but its files left the
 *  handler's folder).
 *  A built-in: `app` (the app's skills), `channel` (this channel's accepted
 *  regeneration) and `regen-waiting`. */
export type HandlerStateKey =
    | 'off' | 'none' | 'review-first' | 'in-use' | 'review-next' | 'orphaned'
    | 'app' | 'channel' | 'regen-waiting';

/** The status dot's colour: green in use, brass waiting, red off, muted
 *  none; `green-waiting` is in use with a newer build waiting. */
export type HandlerTone = 'green' | 'green-waiting' | 'brass' | 'red' | 'muted';

/** Every action a handler card offers. */
export type HandlerAction =
    | 'validate' | 'build' | 'regenerate' | 'review' | 'revert' | 'addExample'
    | 'edit' | 'openFile' | 'openCell' | 'openCard' | 'delete' | 'removeExample';

export interface HandlerState {
    key: HandlerStateKey;
    label: string;
    /** One line under the label: what is in use and what that means. */
    detail: string;
    /** The one action that moves the handler forward, if any. */
    next: HandlerAction | null;
    tone: HandlerTone;
}

/** A build's name: the first 8 characters of its hash. */
export const short = (hash: string | null | undefined): string => (hash ?? '').slice(0, 8);

/** The handler's file types as a phrase: `.a`, `.a or .b`, `.a, .b or .c`. */
export function extPhrase(handler: Pick<HandlerInfo, 'types'>): string {
    const exts = handler.types.map(type => type.ext);
    if (exts.length <= 1) return exts[0] ?? 'its';
    return `${exts.slice(0, -1).join(', ')} or ${exts[exts.length - 1]}`;
}

/** The skills agents get from the build in use, as a parenthesis body. */
function skillsPhrase(handler: HandlerInfo): string {
    const accepted = handler.accepted_skills.join(', ');
    const added = handler.added_skills.join(', ');
    if (accepted && added) return `${accepted} + added ${added}`;
    if (added) return `added ${added}`;
    return accepted || 'none';
}

function userState(handler: HandlerInfo): HandlerState {
    const ext = extPhrase(handler);
    const built = short(handler.built_hash);
    const inUse = short(handler.accepted_derived);
    if (handler.disabled) return {
        key: 'off', label: 'Off', tone: 'red', next: 'edit',
        detail: `Not used: another handler in this channel has the id ‘${handler.id}’. Its files are handled as if it did not exist.`,
    };
    if (!handler.accepted_derived) return handler.has_derived
        ? {
            key: 'review-first', label: 'Build waiting for review', tone: 'brass', next: 'review',
            detail: `Build ${built} is ready. Until you accept it, ${ext} files stay unchecked and agents get no skills.`,
        }
        : {
            key: 'none', label: 'No checks yet', tone: 'muted', next: 'build',
            detail: `Files ending ${ext} are saved unchecked, and agents get no skills for them. Build opens a work stack that writes its checks and skills.`,
        };
    if (handler.accepted) return {
        key: 'in-use', label: 'In use', tone: 'green', next: null,
        detail: `Build ${inUse}: its checks run on every ${ext} file and agents get its skills (${skillsPhrase(handler)}).`,
    };
    return handler.has_derived
        ? {
            key: 'review-next', label: 'In use · new build waiting', tone: 'green-waiting', next: 'review',
            detail: `Build ${inUse} stays in use until you accept build ${built}.`,
        }
        : {
            key: 'orphaned', label: 'In use · build removed from its folder', tone: 'green', next: 'build',
            detail: `Build ${inUse} is still in use, but its files are gone from the handler’s folder. Build again to change it.`,
        };
}

function builtinState(handler: HandlerInfo): HandlerState {
    const ext = extPhrase(handler);
    if (!handler.accepted) return {
        key: 'regen-waiting', label: 'Regenerated skills waiting for review', tone: 'green-waiting', next: 'review',
        detail: `Agents still get ${handler.uses_app_skills ? 'the app’s' : 'this channel’s earlier'} skills until you accept skills ${short(handler.built_hash)}. The checks never change.`,
    };
    return handler.uses_app_skills
        ? {
            key: 'app', label: 'App’s checks and skills', tone: 'green', next: null,
            detail: `The app checks every ${ext} file and agents get the app’s skills (${skillsPhrase(handler)}).`,
        }
        : {
            key: 'channel', label: 'App’s checks · this channel’s skills', tone: 'green', next: null,
            detail: `The app checks ${ext} files. Agents get the skills this channel regenerated (skills ${short(handler.accepted_skills_hash)}).`,
        };
}

/** A handler's status: its label, the line under it, the next action and
 *  the dot's tone. The single source of these words for the handler cards
 *  and their tests. */
export function handlerState(handler: HandlerInfo): HandlerState {
    return handler.builtin ? builtinState(handler) : userState(handler);
}

/** The types this handler shares with another handler, as the red line
 *  under its status says it (neither handles those files); null without a
 *  clash. */
export function clashLine(handler: Pick<HandlerInfo, 'clashes'>): string | null {
    if (!handler.clashes.length) return null;
    const exts = handler.clashes.join(', ');
    return `Shares ${exts} with another handler, so neither handles ${exts} files.`;
}

/** The label with its clash, as the status dot's title says it. */
export function handlerStatus(handler: HandlerInfo): string {
    const { label } = handlerState(handler);
    const clash = clashLine(handler);
    return clash ? `${label}. ${clash}` : label;
}

/** Each action's button label. */
export const ACTION_LABELS: Record<HandlerAction, string> = {
    validate: 'Validate',
    build: 'Build…',
    regenerate: 'Regenerate skills…',
    review: 'Review and accept',
    revert: 'Use the app’s skills',
    addExample: 'Add example',
    edit: 'Edit',
    openFile: 'Open file',
    openCell: 'Open cell',
    openCard: 'Open card',
    delete: 'Delete',
    removeExample: 'Remove',
};

/** The Next line's wording of an action, without a button's trailing
 *  ellipsis (`off` asks for an edit of its id, not just any edit). */
export function nextLabel(handler: HandlerInfo): string | null {
    const { key, next } = handlerState(handler);
    if (!next) return null;
    return key === 'off' ? 'Edit its id' : ACTION_LABELS[next].replace(/…$/, '');
}

/** What an action does, for its button's title and description. */
export function hint(action: HandlerAction, handler: HandlerInfo): string {
    const ext = extPhrase(handler);
    switch (action) {
        case 'validate': return handler.builtin
            ? 'Runs every example through the app’s checks and shows which match what they expect. Changes nothing.'
            : 'Runs every example through the checks of the latest build and shows which match what they expect. Changes nothing.';
        case 'build': return 'Opens a work stack with a copy of this handler and a brief for writing its checks and skills. What is in use does not change until you accept the result here.';
        case 'regenerate': return 'Opens a work stack with this handler, this channel’s examples and its skills, and a brief for rewriting the skills. The app’s checks never change; the skills in use stay until you accept.';
        case 'review': return `Shows the waiting build’s checks and example results, then puts it in use for every ${ext} file and agent.`;
        case 'revert': return 'Drops this channel’s regenerated skills, waiting or accepted. Agents get the app’s skills again.';
        case 'addExample': return 'Copies files you pick into this handler as an example, with the verdict they should get and why. Validate and the next build use it.';
        case 'edit': return 'Change the name, file types, guidance and examples in its .handler file. New guidance is used after the next build is accepted.';
        case 'openFile': return 'Opens the .handler file in a tab of its cell.';
        case 'openCell': return 'Brings the cell that holds this handler to the front of Capabilities.';
        case 'openCard': return 'Brings the built-in card that holds this handler to the front of Capabilities.';
        case 'delete': return 'Removes this handler’s folder (definition, examples and builds) from its cell. Its files are no longer checked.';
        case 'removeExample': return 'Removes this example and its files.';
    }
}

/** Why Validate is unavailable on a user handler that has no build yet. */
export const VALIDATE_NEEDS_BUILD = 'Build first: there are no checks to validate yet';

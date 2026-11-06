// SPDX-License-Identifier: MIT
// ---------------------------------------------------------------------------
// Embeds in the editor: what a `![…](…)` shows, its size and alignment on
// the page, and the toolbar's writes (main.tsx §3c wires them).
//
// An embed is commonmark's inline `image` node or Crepe's `image-block` (an
// image alone in its paragraph), extended with `width` and `align` by
// embed-markdown.ts, which also holds the file encoding. What it shows
// follows the target's extension:
//
//   an image (IMAGE_EXT), or a web URL     Crepe's own image view
//   a video (VIDEO_EXT)                     <video controls>
//   an audio file (AUDIO_EXT)               <audio controls>
//   any other file of the cell              a file card: icon, name, Open
//   a cell path the host does not serve     a missing-file card
//
// The node views are vanilla DOM (ProseMirror owns them), inline styles on
// the skin tokens. This module imports nothing: the editor hands in what it
// needs (the cell's URL and reveal functions, ProseMirror's selection
// classes), so its pure parts run under node:test as they are.
// ---------------------------------------------------------------------------

import { EMBED_MIN_WIDTH, embedAlign, embedWidth } from './embed-markdown';

/** The image extensions: previewed as images, and what Insert writes as `![name](path)`. */
export const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/i;
/** The video extensions an embed plays in a <video>. */
export const VIDEO_EXT = /\.(mp4|webm|ogv|mov|m4v)$/i;
/** The audio extensions an embed plays in an <audio>. */
export const AUDIO_EXT = /\.(mp3|wav|ogg|oga|m4a|flac|aac|opus)$/i;
/** The width presets the toolbar offers, in percent of the column. */
export const EMBED_WIDTH_PRESETS = Object.freeze([25, 50, 75, 100]);

// ---------------------------------------------------------------------------
// Cell paths
// ---------------------------------------------------------------------------

/** The cell-relative path an href in the markdown file at `mdPath` names, or
 *  null when it is external (a scheme, protocol-relative, a pure anchor) or
 *  escapes the cell root. Relative to the file's own directory; a leading
 *  '/' addresses the cell root. */
export function resolveCellPath(mdPath, href) {
    if (!href || typeof href !== 'string') return null;
    if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('//') || href.startsWith('#')) return null;
    const clean = href.split(/[?#]/)[0];
    if (!clean) return null;
    const baseDir = mdPath.includes('/') ? mdPath.slice(0, mdPath.lastIndexOf('/') + 1) : '';
    const joined = clean.startsWith('/') ? clean.slice(1) : baseDir + clean;
    const out = [];
    // Decode each segment BEFORE classifying it, so an encoded '%2e%2e' is
    // treated like a literal '..' and an encoded separator can't smuggle a
    // path through one segment.
    for (const raw of joined.split('/')) {
        let seg;
        try { seg = decodeURIComponent(raw); } catch { seg = raw; }
        if (seg.includes('/') || seg.includes('\\') || seg.includes('\u0000')) return null;
        if (seg === '' || seg === '.') continue;
        if (seg === '..') {
            if (out.length === 0) return null; // escapes the cell — not ours
            out.pop();
            continue;
        }
        out.push(seg);
    }
    return out.length ? out.join('/') : null;
}

/** The last segment of a path or URL, decoded when it decodes. */
export function baseName(path) {
    const clean = String(path || '').split(/[?#]/)[0];
    const last = clean.slice(clean.lastIndexOf('/') + 1);
    try { return decodeURIComponent(last); } catch { return last; }
}

/** What a target previews as by its extension: 'image', 'video', 'audio',
 *  or null for a file with no preview. */
export function previewKind(path) {
    const clean = String(path || '').split(/[?#]/)[0];
    if (IMAGE_EXT.test(clean)) return 'image';
    if (VIDEO_EXT.test(clean)) return 'video';
    if (AUDIO_EXT.test(clean)) return 'audio';
    return null;
}

/** What the embed at `src` shows, in the markdown file at `mdPath`:
 *  `{ kind, cellPath, url, name }`. `fileUrl(cellPath)` is the host's URL
 *  for a file of the cell, or null when the host refuses the path. kind is
 *  'image' (Crepe's view: an image, a web URL, or an empty src awaiting a
 *  link), 'video', 'audio', 'file' (a card) or 'missing' (a cell path the
 *  host does not serve). */
export function resolveEmbed(src, mdPath, fileUrl) {
    const href = typeof src === 'string' ? src : '';
    if (!href) return { kind: 'image', cellPath: null, url: '', name: '' };
    const cellPath = resolveCellPath(mdPath, href);
    if (!cellPath) return { kind: previewKind(href) || 'image', cellPath: null, url: href, name: baseName(href) };
    const url = fileUrl(cellPath);
    const name = baseName(cellPath);
    if (url == null) return { kind: 'missing', cellPath, url: null, name };
    return { kind: previewKind(cellPath) || 'file', cellPath, url, name };
}

/** The URL Crepe's image views load for `src` (their proxyDomURL): a cell
 *  file's served URL, an external URL as written, and '' (no request) for a
 *  cell path the host refuses. Never throws. */
export function proxiedEmbedUrl(src, mdPath, fileUrl) {
    const cellPath = resolveCellPath(mdPath, src);
    if (!cellPath) return src;
    const url = fileUrl(cellPath);
    return url == null ? '' : url;
}

/** Whether a resolved embed takes a size and an alignment (a card does not). */
export function embedSizable(kind) {
    return kind === 'image' || kind === 'video' || kind === 'audio';
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

/** The inline styles of an embed's two boxes, from its layout. A block
 *  embed's wrapper spans the column and places its box (a percent of the
 *  column wide, or as wide as its content) at the alignment. An inline
 *  embed's wrapper is a percent of its paragraph wide: on the line of text,
 *  floated left or right with the text wrapping round it, or centred on a
 *  line of its own. `stretch` (an empty image's link field) fills the
 *  column. Every variant of a wrapper sets the same properties, so a new
 *  layout replaces the old one whole. */
export function embedLayoutStyles(layout, inline, stretch = false) {
    const width = embedWidth(layout && layout.width);
    const align = embedAlign(layout && layout.align);
    const size = width === null ? 'auto' : `${width}%`;
    const box = { display: 'block', position: 'relative', maxWidth: '100%', boxSizing: 'border-box' };
    if (!inline) {
        return {
            wrapper: {
                display: 'flex', flexDirection: 'column',
                alignItems: stretch ? 'stretch'
                    : align === 'center' ? 'center' : align === 'right' ? 'flex-end' : 'flex-start',
            },
            box: { ...box, width: stretch ? 'auto' : size },
        };
    }
    const placed = align === 'center'
        ? { display: 'block', float: 'none', margin: '6px auto', width: width === null ? 'fit-content' : size }
        : align === 'left' || align === 'right'
            ? { display: 'inline-block', float: align, margin: align === 'left' ? '2px 14px 6px 0' : '2px 0 6px 14px', width: size }
            : { display: 'inline-block', float: 'none', margin: '0', width: size };
    return {
        wrapper: { ...placed, maxWidth: '100%', verticalAlign: 'bottom' },
        box: { ...box, width: width === null ? 'auto' : '100%' },
    };
}

function setStyles(el, styles) {
    for (const [name, value] of Object.entries(styles)) el.style[name] = value;
}

/** Lay out a node view's DOM (`dom`, its first child the box) and mark it
 *  for the CSS that sizes Crepe's own image inside (data-embed-width). The
 *  toolbar's size handle calls it for its live preview while dragging. */
export function applyEmbedLayout(dom, layout, stretch = false) {
    const inline = dom.getAttribute('data-embed-inline') === 'true';
    const styles = embedLayoutStyles(layout, inline, stretch);
    setStyles(dom, styles.wrapper);
    if (dom.firstElementChild) setStyles(dom.firstElementChild, styles.box);
    const width = embedWidth(layout && layout.width);
    const align = embedAlign(layout && layout.align);
    if (width === null) dom.removeAttribute('data-embed-width');
    else dom.setAttribute('data-embed-width', String(width));
    if (align === null) dom.removeAttribute('data-embed-align');
    else dom.setAttribute('data-embed-align', align);
}

/** The width (whole percent, EMBED_MIN_WIDTH..100) for the size handle at
 *  `clientX`, from the embed's box and the column it is a percent of, both
 *  measured as the drag began. The handle sits on the edge that moves: a
 *  left-aligned (or unaligned) box grows rightwards from its left edge, a
 *  right-aligned one leftwards from its right edge, and a centred one about
 *  its centre. Null for a column with no width. */
export function draggedWidth(align, box, column, clientX) {
    const base = column.right - column.left;
    if (!(base > 0)) return null;
    let px;
    if (align === 'right') px = box.right - clientX;
    else if (align === 'center') px = 2 * Math.abs(clientX - (box.left + box.right) / 2);
    else px = clientX - box.left;
    return Math.min(100, Math.max(EMBED_MIN_WIDTH, Math.round((px / base) * 100)));
}

// ---------------------------------------------------------------------------
// The toolbar: its target, its place, its writes
// ---------------------------------------------------------------------------

const EMBED_NODES = new Set(['image', 'image-block']);

/** The link around the selection: `{ from, to, href, title, text }` for the
 *  whole run of text carrying the same link mark that holds the selection
 *  (a caret on either edge of the link counts), or null. */
export function linkAround(state) {
    const link = state.schema.marks.link;
    const { $from, $to } = state.selection;
    if (!link || !$from.sameParent($to) || !$from.parent.inlineContent) return null;
    const start = $from.start();
    const kids = [];
    $from.parent.forEach((child, offset) => {
        kids.push({ child, from: start + offset, to: start + offset + child.nodeSize });
    });
    const at = kids.findIndex((k) => k.from <= $from.pos && $to.pos <= k.to && link.isInSet(k.child.marks));
    if (at < 0) return null;
    const mark = link.isInSet(kids[at].child.marks);
    let a = at;
    let b = at;
    while (a > 0 && mark.isInSet(kids[a - 1].child.marks)) a -= 1;
    while (b < kids.length - 1 && mark.isInSet(kids[b + 1].child.marks)) b += 1;
    const from = kids[a].from;
    const to = kids[b].to;
    return { from, to, href: mark.attrs.href, title: mark.attrs.title || null, text: state.doc.textBetween(from, to) };
}

/** What the toolbar is for, or null (no toolbar): a selected embed with a
 *  target, `{ type: 'embed', pos, name, attrs }`, or a link to a file of the cell
 *  that previews (an image, a video, an audio file), `{ type: 'link', from,
 *  to, href, title, text }`. Never while the editor is not editable. */
export function embedToolbarTarget(state, editable, mdPath) {
    if (!editable) return null;
    const node = state.selection.node;
    if (node && EMBED_NODES.has(node.type.name)) {
        // An image with no target yet is Crepe's link field: nothing to size or link.
        if (!node.attrs.src) return null;
        return { type: 'embed', pos: state.selection.from, name: node.type.name, attrs: { ...node.attrs } };
    }
    const link = linkAround(state);
    if (!link) return null;
    const cellPath = resolveCellPath(mdPath, link.href);
    if (!cellPath || !previewKind(cellPath)) return null;
    return { type: 'link', ...link };
}

/** A key that changes whenever the toolbar must redraw for a target. */
export function embedTargetKey(target) {
    return target ? JSON.stringify(target) : '';
}

/** The toolbar's inset inside an embed's box, its gap from a line it sits
 *  below or above, and its margin from the column's edges. */
export const EMBED_TOOLBAR_INSET = 8;
export const EMBED_TOOLBAR_GAP = 6;
export const EMBED_TOOLBAR_MARGIN = 8;
/** The size handle: its width, its tallest, and the least height it keeps
 *  beside a toolbar inside the same box. */
export const EMBED_HANDLE_WIDTH = 8;
export const EMBED_HANDLE_MAX = 36;
export const EMBED_HANDLE_MIN = 20;

/** Whether an embed of `kind` carries the toolbar along its box's top
 *  edge: an image or a video, the selected object itself. An audio player
 *  is all controls and a card all content, so their toolbar sits below
 *  them, as a link's does. */
export function embedHostsToolbar(kind) {
    return kind === 'image' || kind === 'video';
}

/** Where the toolbar goes, relative to the column (its positioned ancestor),
 *  from the target's viewport box (`anchor`), the column's and the
 *  toolbar's own size, so that it never covers the lines around its target.
 *
 *  A box that hosts it (`host`, embedHostsToolbar): along the box's top
 *  edge, inside it, at the edge away from the size handle (the right edge
 *  for a right-aligned box, else the left), and kept in view while a tall
 *  box scrolls under the column's top. When it would sit over the handle
 *  and the box is too short to take the handle below it, it moves past the
 *  handle, beside the box. Anything else (a link, an audio player, a card):
 *  below the target's line, flipping above only when there is no room
 *  below. Either way its left edge stays inside the column. `side` says
 *  which: 'top', 'below' or 'above'. */
export function placeEmbedToolbar(anchor, column, size, { host = false, align = null } = {}) {
    const lowest = column.left + EMBED_TOOLBAR_MARGIN;
    const highest = Math.max(lowest, column.right - EMBED_TOOLBAR_MARGIN - size.width);
    const clampLeft = (x) => Math.max(lowest, Math.min(x, highest));
    if (host) {
        const y = Math.max(anchor.top + EMBED_TOOLBAR_INSET, Math.min(column.top + EMBED_TOOLBAR_MARGIN,
            anchor.bottom - EMBED_TOOLBAR_INSET - EMBED_HANDLE_MIN - size.height));
        const half = EMBED_HANDLE_WIDTH / 2;
        const edge = align === 'right' ? anchor.left : anchor.right;
        let x = align === 'right' ? anchor.right - EMBED_TOOLBAR_INSET - size.width : anchor.left + EMBED_TOOLBAR_INSET;
        const overHandle = x < edge + half && x + size.width > edge - half;
        const roomBelow = anchor.bottom - (y + size.height + EMBED_TOOLBAR_GAP) >= EMBED_HANDLE_MIN;
        if (overHandle && !roomBelow) {
            x = align === 'right' ? edge - half - EMBED_TOOLBAR_GAP - size.width : edge + half + EMBED_TOOLBAR_GAP;
        }
        return { left: clampLeft(x) - column.left, top: y - column.top, side: 'top' };
    }
    const below = anchor.bottom + EMBED_TOOLBAR_GAP;
    const above = anchor.top - EMBED_TOOLBAR_GAP - size.height;
    const flip = below + size.height > column.bottom - EMBED_TOOLBAR_MARGIN && above >= column.top + EMBED_TOOLBAR_MARGIN;
    return { left: clampLeft(anchor.left) - column.left, top: (flip ? above : below) - column.top, side: flip ? 'above' : 'below' };
}

/** Where the size handle goes, relative to the column: on the box's edge
 *  that moves (draggedWidth's), centred in the box's height, or, when the
 *  toolbar (`toolbar`, its viewport box, or null) sits over that edge, in
 *  the part of the box below it. */
export function placeEmbedHandle(anchor, column, align, toolbar) {
    const edge = align === 'right' ? anchor.left : anchor.right;
    const half = EMBED_HANDLE_WIDTH / 2;
    const overEdge = toolbar && toolbar.left < edge + half && toolbar.right > edge - half
        && toolbar.top < anchor.bottom && toolbar.bottom > anchor.top;
    const top = overEdge ? Math.max(anchor.top, toolbar.bottom + EMBED_TOOLBAR_GAP) : anchor.top;
    const span = Math.max(0, anchor.bottom - top);
    const height = Math.min(EMBED_HANDLE_MAX, span);
    return { left: edge - half - column.left, top: top + (span - height) / 2 - column.top, height };
}

/** The transaction that sets an embed's width and/or alignment (`patch`,
 *  either may be null to clear it). The node stays selected. */
export function setEmbedLayout(state, pos, patch) {
    let tr = state.tr;
    if ('width' in patch) tr = tr.setNodeAttribute(pos, 'width', embedWidth(patch.width));
    if ('align' in patch) tr = tr.setNodeAttribute(pos, 'align', embedAlign(patch.align));
    return tr;
}

/** Preview to link: the embed at `pos` becomes its alt text (or, without
 *  one, its file's name) linked to its target, as Insert writes a file it
 *  does not preview; its title or caption becomes the link's title. An
 *  image block becomes a paragraph holding the link. The caret lands at the
 *  link's end, so the toolbar offers Preview to turn it back. */
export function embedToLink(state, pos, TextSelection) {
    const node = state.doc.nodeAt(pos);
    const link = state.schema.marks.link;
    const inline = node.type.name === 'image';
    const label = node.attrs.alt || baseName(node.attrs.src) || node.attrs.src;
    const title = (inline ? node.attrs.title : node.attrs.caption) || null;
    const text = state.schema.text(label, [link.create({ href: node.attrs.src, title })]);
    const replacement = inline ? text : state.schema.nodes.paragraph.create(null, text);
    const tr = state.tr.replaceWith(pos, pos + node.nodeSize, replacement);
    const caret = pos + (inline ? 0 : 1) + label.length;
    return tr.setSelection(TextSelection.create(tr.doc, caret)).removeStoredMark(link).scrollIntoView();
}

/** Link to preview: the link `target` (embedToolbarTarget's) becomes an
 *  embed of its href, its text the alt and its title the title. A link that
 *  is its paragraph's whole content becomes an image block in place of the
 *  paragraph, as the markdown `![text](href)` alone on a line reads back;
 *  any other becomes an inline image. The new embed is selected. */
export function linkToEmbed(state, target, NodeSelection) {
    const { from, to, href, title, text } = target;
    const $from = state.doc.resolve(from);
    const parent = $from.parent;
    const block = state.schema.nodes['image-block'];
    const whole = parent.type.name === 'paragraph' && from === $from.start() && to === $from.end()
        && block && $from.node(-1).canReplaceWith($from.index(-1), $from.index(-1) + 1, block);
    let tr;
    let at;
    if (whole) {
        at = $from.before();
        tr = state.tr.replaceWith(at, $from.after(), block.create({ src: href, caption: title || '', alt: text }));
    } else {
        at = from;
        tr = state.tr.replaceWith(from, to, state.schema.nodes.image.create({ src: href, alt: text, title: title || '' }));
    }
    return tr.setSelection(NodeSelection.create(tr.doc, at)).scrollIntoView();
}

// ---------------------------------------------------------------------------
// The node views
// ---------------------------------------------------------------------------

// Material's description icon (a page) and broken_image, in the card.
const FILE_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M8 16h8v2H8zm0-4h8v2H8zm6-10H6c-1.1 0-2 .9-2 2v16c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm4 18H6V4h7v5h5v11z"/></svg>';
const MISSING_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M21 5v6.59l-3-3.01-4 4.01-4-4-4 4-3-3.01V5c0-1.1.9-2 2-2h14c1.1 0 2 .9 2 2zm-3 6.42 3 3.01V19c0 1.1-.9 2-2 2H5c-1.1 0-2-.9-2-2v-6.58l3 2.99 4-4 4 4 4-3.99z"/></svg>';

function cardDom(resolved, reveal) {
    const missing = resolved.kind === 'missing';
    const card = document.createElement('span');
    card.setAttribute('data-embed-card', missing ? 'missing' : 'file');
    card.title = resolved.cellPath || resolved.url || '';
    setStyles(card, {
        display: 'inline-flex', alignItems: 'center', gap: '8px', maxWidth: '100%', boxSizing: 'border-box',
        padding: '5px 6px 5px 10px', margin: '2px 0', verticalAlign: 'bottom',
        border: missing ? '1px dashed var(--line-2)' : '1px solid var(--line-2)', borderRadius: '6px',
        background: 'var(--crepe-color-surface)', color: missing ? 'var(--ink-3)' : 'var(--crepe-color-on-surface)',
        fontFamily: 'var(--font-ui)', fontSize: '13px', lineHeight: '22px', fontSizeAdjust: 'none',
    });
    const icon = document.createElement('span');
    icon.innerHTML = missing ? MISSING_ICON : FILE_ICON;
    setStyles(icon, { display: 'inline-flex', flex: 'none', color: 'var(--ink-3)' });
    const name = document.createElement('span');
    name.textContent = resolved.name || resolved.cellPath || '';
    setStyles(name, { minWidth: '0', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' });
    card.append(icon, name);
    if (missing) {
        const note = document.createElement('span');
        note.textContent = 'not in this cell';
        setStyles(note, { flex: 'none', fontSize: '11.5px', color: 'var(--ink-3)', paddingRight: '4px' });
        card.append(note);
    } else {
        const open = document.createElement('button');
        open.type = 'button';
        open.textContent = 'Open';
        open.setAttribute('data-embed-action', 'open');
        open.setAttribute('aria-label', `Open ${resolved.name} in a new tab`);
        setStyles(open, {
            flex: 'none', padding: '0 8px', height: '22px', border: '1px solid var(--line-2)', borderRadius: '5px',
            background: 'var(--sheet-3)', color: 'var(--ink)', font: 'inherit', fontSize: '12px', cursor: 'pointer',
        });
        open.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (resolved.cellPath) reveal(resolved.cellPath, { newTab: true });
        });
        card.append(open);
    }
    return card;
}

// A player fills its box edge to edge, as Crepe's image does, so the
// selection ring hugs it alike.
function mediaStyles(kind, layout) {
    const sized = embedWidth(layout.width) !== null;
    return kind === 'video'
        ? { display: 'block', width: sized ? '100%' : '', maxWidth: '100%', height: 'auto', margin: '0',
            borderRadius: '6px', background: 'var(--sheet-3)' }
        : { display: 'block', width: sized ? '100%' : '320px', maxWidth: '100%', margin: '0' };
}

/** The height of a player's native controls bar, along its bottom edge
 *  (Chromium's bar, its timeline above its buttons, with room to spare). */
export const MEDIA_CONTROLS_HEIGHT = 64;

/** Whether a press at `clientY` on the player `media` lands on its picture
 *  rather than its native controls: a video, above its controls bar. An
 *  audio player is all controls. */
export function pressOnMediaBody(media, clientY) {
    if (!media || media.getAttribute('data-embed-media') !== 'video') return false;
    return clientY < media.getBoundingClientRect().bottom - MEDIA_CONTROLS_HEIGHT;
}

function mediaDom(resolved, layout) {
    const el = document.createElement(resolved.kind === 'video' ? 'video' : 'audio');
    el.controls = true;
    el.preload = 'metadata';
    el.src = resolved.url;
    el.setAttribute('data-embed-media', resolved.kind);
    setStyles(el, mediaStyles(resolved.kind, layout));
    return el;
}

/** The editor's embeds: `nodeViews(base)` wraps Crepe's own image views
 *  (`base`, the view's current nodeViews) in views that preview by the
 *  target's kind and lay out by the node's width and align;
 *  `refresh(changedFiles)` re-resolves the live embeds of those files of the
 *  cell (a new revision, or a missing file now served) and touches no
 *  other. `env`: `mdPath`, `fileUrl(cellPath)` (null when refused),
 *  `reveal(cellPath, options)` (the host's api.revealFile; a card's Open
 *  passes `{ newTab: true }`), `NodeSelection`. */
export function createEmbeds(env) {
    const live = new Set();

    const makeView = (crepeView, inline) => (initialNode, view, getPos) => {
        let node = initialNode;
        let inner = null;
        let media = null;
        let bodyKey = null;
        let selected = false;
        let cellPath = null;
        const dom = document.createElement(inline ? 'span' : 'div');
        dom.className = 'colmd-embed';
        dom.setAttribute('data-embed-inline', inline ? 'true' : 'false');
        const box = document.createElement(inline ? 'span' : 'div');
        dom.appendChild(box);

        const selectSelf = () => {
            if (!view.editable) return;
            const pos = getPos();
            if (pos == null) return;
            view.dispatch(view.state.tr.setSelection(env.NodeSelection.create(view.state.doc, pos)));
        };
        // A click on a video's picture selects it (selectSelf, on the press)
        // and leaves playback alone; its native controls keep every click.
        // Read-only, there is no selection and the picture plays as usual.
        const keepPlayback = (event) => {
            if (view.editable && pressOnMediaBody(media, event.clientY)) event.preventDefault();
        };
        // The selection ring: brass, the skin's selection, on the box of
        // every embed alike (Crepe's own marks inside are off, EDITOR_CSS).
        // An image with no target yet is Crepe's link field, not an embed.
        const paintSelected = () => {
            box.style.outline = selected && node.attrs.src ? '2px solid var(--brass)' : 'none';
            box.style.outlineOffset = '2px';
            box.style.borderRadius = '6px';
        };

        const render = () => {
            const resolved = resolveEmbed(node.attrs.src, env.mdPath, env.fileUrl);
            cellPath = resolved.cellPath;
            const layout = { width: node.attrs.width, align: node.attrs.align };
            // Crepe's image view follows its node's src itself; every other
            // body is rebuilt when what it shows changes.
            const key = resolved.kind === 'image' && crepeView ? 'image' : `${resolved.kind}|${resolved.url}|${resolved.name}`;
            if (key !== bodyKey) {
                if (inner && inner.destroy) inner.destroy();
                inner = null;
                media = null;
                box.replaceChildren();
                if (key === 'image') {
                    inner = crepeView(node, view, getPos);
                    box.appendChild(inner.dom);
                    if (selected && inner.selectNode) inner.selectNode();
                } else if (resolved.kind === 'image') {
                    // Crepe's views are absent only if the wiring failed.
                    const img = document.createElement('img');
                    img.src = resolved.url;
                    img.alt = node.attrs.alt || '';
                    setStyles(img, { display: 'block', width: '100%', maxWidth: '100%', height: 'auto' });
                    box.appendChild(img);
                } else if (resolved.kind === 'video' || resolved.kind === 'audio') {
                    media = mediaDom(resolved, layout);
                    media.addEventListener('pointerdown', selectSelf);
                    media.addEventListener('click', keepPlayback);
                    box.appendChild(media);
                } else {
                    box.appendChild(cardDom(resolved, env.reveal));
                }
                bodyKey = key;
            } else if (media) {
                setStyles(media, mediaStyles(resolved.kind, layout));
            }
            dom.setAttribute('data-embed-kind', resolved.kind);
            applyEmbedLayout(dom, embedSizable(resolved.kind) ? layout : { width: null, align: null },
                key === 'image' && !node.attrs.src);
            paintSelected();
        };

        // After its file changes: Crepe's view asks for the image's URL again
        // (a new revision), and a missing file may now be served.
        const self = {
            cellPath: () => cellPath,
            refresh: () => {
                if (inner && inner.update) inner.update(node);
                render();
            },
        };
        live.add(self);
        render();

        return {
            dom,
            update: (next) => {
                if (next.type !== initialNode.type) return false;
                node = next;
                if (inner && inner.update && !inner.update(next)) bodyKey = null;
                render();
                return true;
            },
            selectNode: () => {
                selected = true;
                if (inner && inner.selectNode) inner.selectNode();
                paintSelected();
            },
            deselectNode: () => {
                selected = false;
                if (inner && inner.deselectNode) inner.deselectNode();
                paintSelected();
            },
            // The player's own controls and the card's button keep their
            // events (seeking never starts a node drag); a press on the
            // player selects the embed itself (selectSelf), and a click on a
            // video's picture leaves playback alone (keepPlayback).
            stopEvent: (event) => {
                const target = event.target;
                if (media && target && media.contains(target)) return true;
                if (target && target.closest && target.closest('[data-embed-action]')) return true;
                return inner && inner.stopEvent ? inner.stopEvent(event) : false;
            },
            ignoreMutation: (mutation) => mutation.type !== 'selection',
            destroy: () => {
                live.delete(self);
                if (inner && inner.destroy) inner.destroy();
                inner = null;
            },
        };
    };

    return {
        nodeViews: (base) => ({
            image: makeView(base && base.image, true),
            'image-block': makeView(base && base['image-block'], false),
        }),
        refresh: (changedFiles) => {
            const changed = new Set(Array.isArray(changedFiles) ? changedFiles : []);
            for (const view of live) if (changed.has(view.cellPath())) view.refresh();
        },
    };
}

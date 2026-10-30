// SPDX-License-Identifier: MIT
// ---------------------------------------------------------------------------
// Embeds in markdown: how an image (or any `![…](…)` embed) keeps its alt,
// its caption, its size and its alignment in the file.
//
//   ![plot](charts/plot.png)                               a preview
//   ![plot](charts/plot.png "Weekly totals")               with a caption
//   ![plot](charts/plot.png){width=60% align=center}       sized and aligned
//   [plot](charts/plot.png)                                a link, no preview
//
// The `!` makes a preview and a plain link stays a link. Size and alignment
// live in a pandoc-style attribute block directly after the image's closing
// parenthesis (no space): `{` `width=NN%` and/or `align=left|center|right`,
// separated by spaces, each at most once, `}`. NN is a whole percent of the
// text column, 1 to 100. The block is written only when one of them is set,
// width first, and written raw (never escaped). Anything else in braces
// after an image is text, as it always was.
//
// Milkdown gives the image two nodes: commonmark's inline `image` (src, alt,
// title) and, for an image alone in its paragraph, Crepe's `image-block`
// (src, caption, and a height ratio it wrote INTO the alt as `![1.00](…)`).
// Both nodes are extended here with `width` and `align`; the image block
// keeps the real alt (the filename Insert writes, or the author's) and no
// ratio, and its title stays the caption. The remark transform below takes
// the attribute block off the text that follows an image and turns an image
// alone in its paragraph into an image block itself, carrying the alt and
// the attributes, so it never depends on running before or after Crepe's
// remark-image-block (which would drop both).
//
// This module is shared by the milkdown renderer's editor (main.tsx) and the
// snapshot codec (sdk/scripts/snapshot-codecs/milkdown.ts), which must
// serialize exactly what the editor does: it imports nothing, and each schema
// extension takes the schema it extends. The canonical copy is the renderer
// runtime's sdk/scripts/snapshot-codecs/embed-markdown.ts; the renderer
// carries a byte-identical copy (sdk/tests/test_renderer_runtime.py pins it).
// A change changes the approved codec hash: rebuild it with
// `node scripts/build_snapshot_codecs.mjs`, then the runtime manifest with
// `node scripts/build_renderer_runtime.mjs` (both from sdk/).
// ---------------------------------------------------------------------------

export const EMBED_ALIGNS = Object.freeze(['left', 'center', 'right']);
/** The narrowest width the size handle writes, in percent. */
export const EMBED_MIN_WIDTH = 10;

export type EmbedLayout = { width: number | null; align: string | null };

const BLOCK = /^\{([^{}\n]*)\}/;

/** The attribute block at the start of `text`: its width, align and length
 *  in characters, or null when `text` does not start with a valid one. */
export function parseEmbedBlock(text: unknown): (EmbedLayout & { length: number }) | null {
    if (typeof text !== 'string') return null;
    const match = BLOCK.exec(text);
    if (!match) return null;
    const tokens = match[1].trim().split(/\s+/).filter(Boolean);
    if (tokens.length === 0) return null;
    let width = null;
    let align = null;
    for (const token of tokens) {
        const w = /^width=(\d{1,3})%$/.exec(token);
        const a = /^align=(left|center|right)$/.exec(token);
        if (w && width === null && Number(w[1]) >= 1 && Number(w[1]) <= 100) width = Number(w[1]);
        else if (a && align === null) align = a[1];
        else return null;
    }
    return { width, align, length: match[0].length };
}

/** The attribute block for a layout: '' when neither is set. */
export function formatEmbedBlock(layout: Partial<EmbedLayout> | null | undefined): string {
    const width = embedWidth(layout && layout.width);
    const align = embedAlign(layout && layout.align);
    const parts = [];
    if (width !== null) parts.push(`width=${width}%`);
    if (align !== null) parts.push(`align=${align}`);
    return parts.length ? `{${parts.join(' ')}}` : '';
}

/** A stored width as the encoding allows it: a whole percent in 1..100, or null. */
export function embedWidth(value: unknown): number | null {
    return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 100 ? value : null;
}

/** A stored alignment as the encoding allows it, or null. */
export function embedAlign(value: unknown): string | null {
    return typeof value === 'string' && EMBED_ALIGNS.includes(value) ? value : null;
}

// Where the transform leaves an image's layout on its mdast node.
const DATA_KEY = 'colmdEmbed';

function layoutOf(node: any): EmbedLayout {
    const layout = node && node.data && node.data[DATA_KEY];
    return { width: embedWidth(layout && layout.width), align: embedAlign(layout && layout.align) };
}

/** The remark transform (mdast, in place): an attribute block in the text
 *  right after an image moves onto the image, and a paragraph whose only
 *  child is an image becomes an `image-block` node carrying its url, alt,
 *  title and layout, exactly where Crepe's remark-image-block would put one. */
export function embedAttributesTransform(tree: any): void {
    const visit = (parent: any) => {
        const children = parent && parent.children;
        if (!Array.isArray(children)) return;
        for (let i = 0; i < children.length; i++) {
            const node = children[i];
            if (node.type === 'image') {
                const next = children[i + 1];
                const block = next && next.type === 'text' ? parseEmbedBlock(next.value) : null;
                if (block) {
                    node.data = { ...(node.data || {}), [DATA_KEY]: { width: block.width, align: block.align } };
                    next.value = next.value.slice(block.length);
                    if (!next.value) children.splice(i + 1, 1);
                }
                continue;
            }
            visit(node);
            if (node.type === 'paragraph' && node.children.length === 1 && node.children[0].type === 'image') {
                const image = node.children[0];
                children[i] = {
                    type: 'image-block', url: image.url, alt: image.alt, title: image.title,
                    data: image.data, position: node.position,
                };
            }
        }
    };
    visit(tree);
}

/** The remark plugin form of the transform (unified's attacher). */
export function remarkEmbedAttributes() {
    return embedAttributesTransform;
}

function addAttributeBlock(state: any, attrs: any) {
    const block = formatEmbedBlock(attrs);
    // An html node is written as is: the block is never escaped.
    if (block) state.addNode('html', undefined, block);
}

const LAYOUT_ATTRS = {
    width: { default: null, validate: 'number|null' },
    align: { default: null, validate: 'string|null' },
};

function domLayout(dom: any): EmbedLayout {
    const width = Number.parseInt(dom.getAttribute('data-width') || '', 10);
    return { width: embedWidth(width), align: embedAlign(dom.getAttribute('data-align')) };
}

function layoutDom(attrs: any) {
    const out: Record<string, string> = {};
    if (embedWidth(attrs.width) !== null) out['data-width'] = String(attrs.width);
    if (embedAlign(attrs.align) !== null) out['data-align'] = attrs.align;
    return out;
}

/** commonmark's inline `image` with a layout (imageSchema.extendSchema). */
export function extendImageSchema(prev: (ctx: any) => any) {
    return (ctx: any) => {
        const base = prev(ctx);
        return {
            ...base,
            attrs: { ...base.attrs, ...LAYOUT_ATTRS },
            parseDOM: [{
                tag: 'img[src]',
                getAttrs: (dom: any) => ({
                    src: dom.getAttribute('src') || '',
                    alt: dom.getAttribute('alt') || '',
                    title: dom.getAttribute('title') || dom.getAttribute('alt') || '',
                    ...domLayout(dom),
                }),
            }],
            toDOM: (node: any) => ['img', {
                src: node.attrs.src, alt: node.attrs.alt, title: node.attrs.title, ...layoutDom(node.attrs),
            }],
            parseMarkdown: {
                match: ({ type }: any) => type === 'image',
                runner: (state: any, node: any, type: any) => {
                    // mdast gives null for an omitted alt or title; both are
                    // validated string attributes (prosemirror-model checks them
                    // in NodeType.create).
                    state.addNode(type, { src: node.url, alt: node.alt ?? '', title: node.title ?? '', ...layoutOf(node) });
                },
            },
            toMarkdown: {
                match: (node: any) => node.type.name === 'image',
                runner: (state: any, node: any) => {
                    state.addNode('image', undefined, undefined, {
                        title: node.attrs.title, url: node.attrs.src, alt: node.attrs.alt,
                    });
                    addAttributeBlock(state, node.attrs);
                },
            },
        };
    };
}

/** Crepe's `image-block` with the real alt and a layout, and no height
 *  ratio (imageBlockSchema.extendSchema). */
export function extendImageBlockSchema(prev: (ctx: any) => any) {
    return (ctx: any) => {
        const base = prev(ctx);
        const { ratio: _ratio, ...attrs } = base.attrs;
        return {
            ...base,
            attrs: { ...attrs, alt: { default: '', validate: 'string' }, ...LAYOUT_ATTRS },
            parseDOM: [{
                tag: 'img[data-type="image-block"]',
                getAttrs: (dom: any) => ({
                    src: dom.getAttribute('src') || '',
                    caption: dom.getAttribute('caption') || '',
                    alt: dom.getAttribute('alt') || '',
                    ...domLayout(dom),
                }),
            }],
            toDOM: (node: any) => ['img', {
                'data-type': 'image-block', src: node.attrs.src, caption: node.attrs.caption,
                alt: node.attrs.alt, ...layoutDom(node.attrs),
            }],
            parseMarkdown: {
                match: ({ type }: any) => type === 'image-block',
                runner: (state: any, node: any, type: any) => {
                    // mdast gives null for an omitted title; caption is a
                    // validated string attribute.
                    state.addNode(type, { src: node.url, caption: node.title ?? '', alt: node.alt || '', ...layoutOf(node) });
                },
            },
            toMarkdown: {
                match: (node: any) => node.type.name === 'image-block',
                runner: (state: any, node: any) => {
                    state.openNode('paragraph');
                    state.addNode('image', undefined, undefined, {
                        title: node.attrs.caption, url: node.attrs.src, alt: node.attrs.alt,
                    });
                    addAttributeBlock(state, node.attrs);
                    state.closeNode();
                },
            },
        };
    };
}

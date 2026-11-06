// The markdown editor (the SDK's milkdown renderer) writes an embed's size and
// alignment as an attribute block directly after the image:
//
//   ![plot](plot.png){width=60% align=center}
//
// `{` then `width=N%` (N a whole percent, 1..100) and/or
// `align=left|center|right`, space-separated, each at most once, `}`. The
// content/renderers/documents/milkdown/embed-markdown.ts owns that grammar;
// this is its read side for the app's react-markdown previews. A valid block
// becomes the image's inline style and leaves the text; anything else in
// braces stays text, as it does in the editor.

interface MdNode {
    type: string;
    value?: string;
    children?: MdNode[];
    data?: { hProperties?: Record<string, unknown> } & Record<string, unknown>;
}

export interface EmbedLayout {
    width: number | null;
    align: 'left' | 'center' | 'right' | null;
}

const BLOCK = /^\{([^{}\n]*)\}/;

/** The attribute block at the start of `text` and its length, or null when
 *  `text` does not start with a valid one. */
export function parseEmbedBlock(text: string): (EmbedLayout & { length: number }) | null {
    const match = BLOCK.exec(text);
    if (!match) return null;
    const tokens = match[1].trim().split(/\s+/).filter(Boolean);
    if (tokens.length === 0) return null;
    let width: number | null = null;
    let align: EmbedLayout['align'] = null;
    for (const token of tokens) {
        const w = /^width=(\d{1,3})%$/.exec(token);
        const a = /^align=(left|center|right)$/.exec(token);
        if (w && width === null && Number(w[1]) >= 1 && Number(w[1]) <= 100) width = Number(w[1]);
        else if (a && align === null) align = a[1] as EmbedLayout['align'];
        else return null;
    }
    return { width, align, length: match[0].length };
}

/** The inline style for an image's layout. `alone` is an image that is its
 *  paragraph's only content (the editor's block embed): it aligns on its own
 *  line, while an image inside text floats left or right. */
export function embedStyle({ width, align }: EmbedLayout, alone: boolean): string {
    const rules: string[] = [];
    if (width !== null) rules.push(`width:${width}%`);
    if (align === 'center') rules.push('display:block', 'margin-left:auto', 'margin-right:auto');
    else if (align === 'right') rules.push(...(alone ? ['display:block', 'margin-left:auto'] : ['float:right']));
    else if (align === 'left' && !alone) rules.push('float:left');
    return rules.join(';');
}

function visit(parent: MdNode): void {
    const children = parent.children;
    if (!children) return;
    for (let i = 0; i < children.length; i++) {
        const node = children[i];
        if (node.type !== 'image') {
            visit(node);
            continue;
        }
        const next = children[i + 1];
        const block = next && next.type === 'text' && next.value ? parseEmbedBlock(next.value) : null;
        if (!block) continue;
        next.value = next.value!.slice(block.length);
        if (!next.value) children.splice(i + 1, 1);
        const alone = parent.type === 'paragraph' && children.length === 1;
        const style = embedStyle(block, alone);
        if (style) node.data = { ...node.data, hProperties: { ...node.data?.hProperties, style } };
    }
}

/** remark plugin: moves each valid attribute block onto its image's style. */
export function remarkEmbedAttributes() {
    return (tree: MdNode) => visit(tree);
}

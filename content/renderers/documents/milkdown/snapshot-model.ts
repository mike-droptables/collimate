// Shared by the milkdown renderer and its snapshot codec: the canonical copy
// is the renderer runtime's sdk/scripts/snapshot-codecs/snapshot-model.ts; the
// renderer carries a byte-identical copy (sdk/tests/test_renderer_runtime.py
// pins it).
// Restore a snapshot through the constructor Milkdown's parser uses,
// NodeType.create, which validates every attribute (prosemirror-model 1.25.12).
// Validate the document shape without coercing attributes or changing
// serialized bytes: a value of another type is rejected, never repaired.
export function restoreProseMirrorSnapshot(schema, payload) {
    if (typeof payload !== 'string') throw new Error('Invalid encoded document snapshot');
    const snapshot = JSON.parse(payload);
    const decode = (value) => {
        if (!value || typeof value !== 'object' || typeof value.type !== 'string') {
            throw new Error('Invalid document snapshot node');
        }
        if (value.marks != null && !Array.isArray(value.marks)) throw new Error('Invalid snapshot marks');
        const marks = (value.marks || []).map(mark => {
            const type = schema.marks[mark?.type];
            if (!type) throw new Error('Unknown snapshot mark');
            return type.create(mark.attrs);
        });
        if (value.type === 'text') {
            if (typeof value.text !== 'string') throw new Error('Invalid snapshot text');
            return schema.text(value.text, marks);
        }
        const type = schema.nodes[value.type];
        if (!type) throw new Error('Unknown snapshot node');
        if (value.content != null && !Array.isArray(value.content)) throw new Error('Invalid snapshot content');
        const node = type.create(value.attrs, (value.content || []).map(decode), marks);
        if (!type.validContent(node.content)) throw new Error('Invalid snapshot node content');
        return node;
    };
    const doc = decode(snapshot);
    if (doc.type !== schema.topNodeType) throw new Error('Snapshot is not a document');
    return doc;
}

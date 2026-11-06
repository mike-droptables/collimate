// Where an admitted package came from, as the UI names it
// (api/collimate_server/provenance.py: an upstream kind and its id).

export type UpstreamKind = 'app' | 'catalog' | 'channel' | 'cell' | 'zip';

const LABELS: Record<UpstreamKind, string> = {
    app: 'The app’s content',
    catalog: 'Your catalog',
    channel: 'Another channel',
    cell: 'A cell of this channel',
    zip: 'A file',
};

/** The upstream kind in words ('The app’s content', 'Your catalog', …). */
export const upstreamLabel = (kind: UpstreamKind): string => LABELS[kind];

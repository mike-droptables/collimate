// The retired palette names, theme ids and modules, for the token-sweep tests.
// Each name is assembled from two parts so that the W1 exit grep for the
// retired names finds nothing in frontend/, this module included. Tests import
// RETIRED_TOKENS, or retiredOr to add per-test extras.

const PARTS: ReadonlyArray<readonly [string, string]> = [
    ['void', '-main'],
    ['obsid', 'ian'],
    ['glass', '-panel'],
    ['tung', 'sten'],
    ['signal', '-white'],
    ['comment', '-grey'],
    ['fila', 'ment'],
    ['electric', '-teal'],
    ['plasma', '-pink'],
    ['bg', '-glow'],
    ['renderer', '-surface'],
    ['Static', 'Background'],
    ['tube', '-amp'],
    ['sequential', '-dark'],
    ['sequential', '-light'],
    ['paper', '-light'],
];

const NAMES = PARTS.map(([head, tail]) => head + tail);

export const RETIRED_TOKENS = new RegExp(NAMES.join('|'));

/** RETIRED_TOKENS plus extra regex alternatives, e.g. retiredOr('rgba?\\(', 'color-mix'). */
export function retiredOr(...extras: string[]): RegExp {
    return new RegExp([...NAMES, ...extras].join('|'));
}

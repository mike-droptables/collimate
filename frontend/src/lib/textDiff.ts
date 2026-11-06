// Dependency-free line diff for the "Compare with…" tab's fallback view
// (used when a file's renderer lacks supports_diff). Classic LCS via dynamic
// programming, with a common prefix/suffix trim first so typical "same file,
// small edit" comparisons stay tiny. Above LCS_CELL_CAP the middle section is
// emitted as a whole-block replacement instead — O(n·m) memory on two huge,
// mostly-different files would hang the tab for a worse answer.

export interface DiffRow {
    kind: 'same' | 'del' | 'add';
    /** Line text per side; null = no line on that side for this row. */
    left: string | null;
    right: string | null;
    /** 1-based line numbers per side; null when the side has no line. */
    leftNo: number | null;
    rightNo: number | null;
}

const LCS_CELL_CAP = 4_000_000;

/** Longest-common-subsequence pairing of two line arrays, as index pairs
 *  [i, j] in ascending order. */
function lcsPairs(a: string[], b: string[]): Array<[number, number]> {
    const n = a.length;
    const m = b.length;
    // DP table of LCS lengths, flattened. Uint32 keeps it compact.
    const width = m + 1;
    const dp = new Uint32Array((n + 1) * width);
    for (let i = n - 1; i >= 0; i--) {
        for (let j = m - 1; j >= 0; j--) {
            dp[i * width + j] = a[i] === b[j]
                ? dp[(i + 1) * width + j + 1] + 1
                : Math.max(dp[(i + 1) * width + j], dp[i * width + j + 1]);
        }
    }
    const pairs: Array<[number, number]> = [];
    let i = 0, j = 0;
    while (i < n && j < m) {
        if (a[i] === b[j]) { pairs.push([i, j]); i++; j++; }
        else if (dp[(i + 1) * width + j] >= dp[i * width + j + 1]) i++;
        else j++;
    }
    return pairs;
}

/** Diff two texts line-by-line. Rows come back in display order: unchanged
 *  lines as 'same' rows, changed regions as a run of 'del' rows (base side)
 *  followed by a run of 'add' rows (current side). */
export function diffLines(base: string, current: string): DiffRow[] {
    const a = base.split('\n');
    const b = current.split('\n');
    const rows: DiffRow[] = [];

    // Trim the common prefix/suffix — the cheap 99% case.
    let start = 0;
    while (start < a.length && start < b.length && a[start] === b[start]) start++;
    let endA = a.length, endB = b.length;
    while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }

    const pushSame = (i: number, j: number) =>
        rows.push({ kind: 'same', left: a[i], right: b[j], leftNo: i + 1, rightNo: j + 1 });
    const pushRange = (fromA: number, toA: number, fromB: number, toB: number) => {
        for (let i = fromA; i < toA; i++)
            rows.push({ kind: 'del', left: a[i], right: null, leftNo: i + 1, rightNo: null });
        for (let j = fromB; j < toB; j++)
            rows.push({ kind: 'add', left: null, right: b[j], leftNo: null, rightNo: j + 1 });
    };

    for (let k = 0; k < start; k++) pushSame(k, k);

    const midA = a.slice(start, endA);
    const midB = b.slice(start, endB);
    if (midA.length > 0 || midB.length > 0) {
        if ((midA.length + 1) * (midB.length + 1) > LCS_CELL_CAP) {
            // Too big for the DP table — whole-block replacement.
            pushRange(start, endA, start, endB);
        } else {
            let ai = start, bj = start;
            for (const [pi, pj] of lcsPairs(midA, midB)) {
                const i = start + pi, j = start + pj;
                pushRange(ai, i, bj, j);
                pushSame(i, j);
                ai = i + 1; bj = j + 1;
            }
            pushRange(ai, endA, bj, endB);
        }
    }

    for (let k = 0; endA + k < a.length; k++) pushSame(endA + k, endB + k);
    return rows;
}

/** The rows in the order a stacked (one-column) view reads them, the
 *  current side first: each changed run's 'add' rows (the current side's
 *  lines) ahead of its 'del' rows (the base side's), unchanged rows where
 *  they were. */
export function currentFirst(rows: DiffRow[]): DiffRow[] {
    const out: DiffRow[] = [];
    let dels: DiffRow[] = [];
    const flush = () => { for (const d of dels) out.push(d); dels = []; };
    for (const r of rows) {
        if (r.kind === 'del') dels.push(r);
        else if (r.kind === 'add') out.push(r);
        else { flush(); out.push(r); }
    }
    flush();
    return out;
}

/** Counts for the fallback view's header chip. */
export function diffStats(rows: DiffRow[]): { added: number; removed: number } {
    let added = 0, removed = 0;
    for (const r of rows) {
        if (r.kind === 'add') added++;
        else if (r.kind === 'del') removed++;
    }
    return { added, removed };
}

import React, { useState, useMemo, useDeferredValue } from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, tokens, useCollimateFile } from '/api/sdk/react.js';

const { color, font } = tokens;

function parseCsv(text, sep) {
  const rows = [];
  let i = 0, field = '', row = [], inQuotes = false;
  while (i < text.length) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
        inQuotes = false; i++; continue;
      }
      field += c; i++; continue;
    }
    if (c === '"') { inQuotes = true; i++; continue; }
    if (c === sep) { row.push(field); field = ''; i++; continue; }
    if (c === '\n' || c === '\r') {
      row.push(field); rows.push(row); field = ''; row = [];
      if (c === '\r' && text[i + 1] === '\n') i += 2; else i++;
      continue;
    }
    field += c; i++;
  }
  if (field.length > 0 || row.length > 0) { row.push(field); rows.push(row); }
  return rows.filter((r) => !(r.length === 1 && r[0] === ''));
}

function scoreboardToTable(obj) {
  const rubric = Array.isArray(obj.rubric) ? obj.rubric : [];
  const headers = ['name', ...rubric, 'total', 'notes'];
  const rows = (obj.rows || []).map((r) => {
    const out = [String(r.name || '')];
    for (const k of rubric) {
      const v = r.scores && r.scores[k];
      out.push(v === undefined || v === null ? '' : String(v));
    }
    out.push(r.total === undefined ? '' : String(r.total));
    out.push(String(r.notes || ''));
    return out;
  });
  return { headers, rows, verdict: obj.verdict || '', winner: obj.winner || '' };
}

// Blank cells parse as NaN (not 0) so they sort with the non-numeric cells.
function toNum(v) {
  const s = String(v ?? '').trim();
  return s === '' ? NaN : Number(s);
}

const collator = new Intl.Collator();

function looksNumeric(values) {
  let n = 0, total = 0;
  for (const v of values) {
    if (v === '' || v == null) continue;
    total++;
    if (!isNaN(Number(v))) n++;
  }
  return total > 0 && n / total > 0.7;
}

const PAGE_SIZE = 1000;
export const TABLE_ROOT = Object.freeze({ width: '100%', height: '100%', background: color.sheet, color: color.ink, fontFamily: font.ui, display: 'flex', flexDirection: 'column', overflow: 'hidden' });
export const TABLE_SCROLL = Object.freeze({ flex: 1, minHeight: 0, overflow: 'auto' });
const wrap = TABLE_ROOT;
// The head row and the bars above it are raised rows (sheet-2); the column
// heads sit on a line-2 rule in ink-2, the cells on line hairlines.
const bar = { background: color.sheet2, borderBottom: `1px solid ${color.line}` };
const th = {
    textAlign: 'left', padding: '8px 12px', background: color.sheet2,
    borderBottom: `1px solid ${color.line2}`, fontSize: 12, fontWeight: 600,
    color: color.ink2, cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap',
  };
const td = { padding: '7px 12px', borderBottom: `1px solid ${color.line}`, fontSize: 13, color: color.ink };
// Zebra striping mixes 4% of the ink into the ground, so it reads on a
// dark or a light skin alike.
const zebraBg = `color-mix(in srgb, ${color.ink} 4%, transparent)`;

const tdText = { ...td, textAlign: 'left', fontVariantNumeric: 'normal' };
const tdNumber = { ...td, textAlign: 'right', fontVariantNumeric: 'tabular-nums' };
const rowStyles = [{ background: 'transparent' }, { background: zebraBg }];

/** The table's style objects, exported for the model test. */
export const TABLE_STYLES = Object.freeze({ wrap, bar, th, td, tdText, tdNumber, rowStyles, zebraBg });

function TableView() {
  const { content, fileName } = useCollimateFile();
  const [sortCol, setSortCol] = useState(-1);
  const [sortDir, setSortDir] = useState(1);
  const [filter, setFilter] = useState('');
  const deferredFilter = useDeferredValue(filter);

  const parsed = useMemo(() => {
    const name = (fileName || '').toLowerCase();
    // Any .json (including .scoreboard.json) is tried as a scoreboard first.
    if (name.endsWith('.json')) {
      try {
        const obj = JSON.parse(content || '{}');
        if (obj && (obj.rubric || obj.rows)) return scoreboardToTable(obj);
      } catch (e) { /* fall through */ }
    }
    const sep = name.endsWith('.tsv') ? '\t' : ',';
    const rows = parseCsv(content || '', sep);
    if (rows.length === 0) return { headers: [], rows: [] };
    return { headers: rows[0], rows: rows.slice(1) };
  }, [content, fileName]);

  const numericCols = useMemo(() => {
    const out = new Set();
    parsed.headers && parsed.headers.forEach((_, ci) => {
      if (looksNumeric(parsed.rows.map((r) => r[ci]))) out.add(ci);
    });
    return out;
  }, [parsed]);

  const filteredRows = useMemo(() => {
    const needle = deferredFilter.trim().toLowerCase();
    if (!needle) return parsed.rows;
    return parsed.rows.filter((r) => r.some((cell) => String(cell ?? '').toLowerCase().includes(needle)));
  }, [parsed, deferredFilter]);

  const sortedRows = useMemo(() => {
    if (sortCol < 0) return filteredRows;
    const isNum = numericCols.has(sortCol);
    return [...filteredRows].sort((a, b) => {
      const av = a[sortCol] ?? '';
      const bv = b[sortCol] ?? '';
      if (isNum) {
        const an = toNum(av), bn = toNum(bv);
        const aBad = Number.isNaN(an), bBad = Number.isNaN(bn);
        // Blank and non-numeric cells go after the numbers in either direction.
        if (aBad || bBad) {
          if (aBad && bBad) return collator.compare(String(av), String(bv)) * sortDir;
          return aBad ? 1 : -1;
        }
        return (an - bn) * sortDir;
      }
      return collator.compare(String(av), String(bv)) * sortDir;
    });
  }, [filteredRows, sortCol, sortDir, numericCols]);

  const [page, setPage] = useState({ rows: null, count: PAGE_SIZE });
  // A new result set starts capped in its first render, including external file
  // changes. An effect reset would first paint the old expanded row count.
  // Forget the old expansion so clearing a filter cannot restore it.
  if (page.rows !== sortedRows) setPage({ rows: sortedRows, count: PAGE_SIZE });
  const limit = page.rows === sortedRows ? page.count : PAGE_SIZE;
  const visibleRows = sortedRows.slice(0, limit);
  const remaining = sortedRows.length - visibleRows.length;

  function clickHeader(i) {
    if (sortCol === i) setSortDir((d) => -d);
    else { setSortCol(i); setSortDir(1); }
  }


  if (parsed.headers.length === 0) {
    return (
      <div style={{ ...wrap, alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ color: color.ink3, fontSize: 13 }}>
          No tabular data — expected CSV/TSV or scoreboard JSON
        </div>
      </div>
    );
  }

  return (
    <div style={wrap}>
      {parsed.verdict && (
        <div style={{ ...bar, padding: '12px 16px' }}>
          {parsed.winner && (
            <div style={{ fontSize: 11, color: color.ink3, marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.08em' }}>winner</div>
          )}
          {parsed.winner && (
            <div style={{ fontSize: 16, fontWeight: 600, color: color.ink, marginBottom: 6 }}>{parsed.winner}</div>
          )}
          <div style={{ fontSize: 13, color: color.ink2, lineHeight: 1.5 }}>{parsed.verdict}</div>
        </div>
      )}
      {parsed.rows.length > 0 && (
        <div style={{ ...bar, display: 'flex', alignItems: 'center', gap: 12, padding: '6px 12px' }}>
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="filter rows…"
            spellCheck={false}
            style={{ flex: '0 1 220px', background: color.sheet, color: color.ink, border: `1px solid ${color.line}`, borderRadius: `calc(${tokens.radius} - 2px)`, padding: '4px 8px', fontSize: 12.5, fontFamily: font.ui, outline: 'none' }}
          />
          <span style={{ fontSize: 11.5, fontFamily: font.mono, color: color.ink3, whiteSpace: 'nowrap' }}>
            {deferredFilter.trim() ? `${filteredRows.length}/${parsed.rows.length} rows` : `${parsed.rows.length} rows`}
          </span>
        </div>
      )}
      <div style={TABLE_SCROLL}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {parsed.headers.map((h, i) => (
                <th key={i} style={th} onClick={() => clickHeader(i)}>
                  {String(h)}
                  {sortCol === i ? (sortDir > 0 ? ' ▲' : ' ▼') : ''}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((row, ri) => (
              <tr key={ri} style={rowStyles[ri % 2]}>
                {parsed.headers.map((_, ci) => (
                  <td key={ci} style={numericCols.has(ci) ? tdNumber : tdText}>
                    {row[ci] ?? ''}
                  </td>
                ))}
              </tr>
            ))}
            {sortedRows.length === 0 && (
              <tr><td style={{ ...td, color: color.ink3 }} colSpan={parsed.headers.length || 1}>
                {deferredFilter.trim() ? `no rows match “${deferredFilter.trim()}”` : '(empty)'}
              </td></tr>
            )}
          </tbody>
        </table>
        {remaining > 0 && <button type="button"
          style={{ margin: 12, height: 24, padding: '0 9px', color: color.ink2, background: 'transparent', border: `1px solid ${color.line2}`, borderRadius: `calc(${tokens.radius} - 3px)`, fontSize: 12.5, fontFamily: font.ui, cursor: 'pointer' }}
          onClick={() => setPage({ rows: sortedRows, count: limit + PAGE_SIZE })}>
          Show {Math.min(PAGE_SIZE, remaining)} more
        </button>}
      </div>
    </div>
  );
}

export default defineRenderer({ component: TableView });

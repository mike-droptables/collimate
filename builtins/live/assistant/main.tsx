/**
 * Assistant — the chat renderer (binds `.chat`), powered by assistant-ui.
 *
 * The `.chat` file (a run UI file at `_runs/<label>/chat/conversation.chat`,
 * revealed by the run, never pinned) is a session pointer
 * (collimate/pointers.py `chat_session_pointer`):
 *
 *   { chat_id, ws_path, token, label, threads_dir, active_thread,
 *     threads: [{id, title, file}] }
 *
 * Before the run's process exists the host writes its starting
 * placeholder there instead, `{"starting": true, "label"}`
 * (collimate/pointers.py `starting_pointer`): the chat shows at once with its
 * composer open, and what is sent queues (message-model's module-level
 * queue, keyed by the file, so it outlives the remount onto the real
 * pointer) as queued bubbles, sent in order as `user_msg` frames once the
 * real session's attach is confirmed (its socket opened AND the host sent a
 * frame on it — an attach the host refuses with 4409/4404/4401 is accepted
 * first, then closed, so an open alone proves nothing). A queue the run never
 * takes goes back into the composer with a banner.
 *
 * One conversation per run (each run's pointer lives in its own run folder,
 * `_runs/<label>/chat/conversation.chat`, so several chats can share a cell):
 * the renderer serves the pointer's active thread only, read from the path
 * the pointer names; the threads list names available transcript files.
 *
 * The assistant-ui ExternalStoreRuntime receives events from the Collimate
 * chat WebSocket at `ws_path`. History is the conversation's open-format
 * transcript at its `file` (collimate/transcript.py).
 *
 * The transcript reflects the attached generator's conversation. A stopped
 * run's conversation is saved in its log and the composer closes.
 *
 * Rendered by the host's NativeRenderer (shadow DOM, main document) — see the
 * manifest's `embed: native`. That is deliberate: a live chat holds a WebSocket,
 * and native renderers unmount cleanly (socket closes) on tab-away and persist
 * across file-content changes without remounting. It ships as `.tsx`,
 * compiled by the shared renderer pipeline; CDN imports remain external. React + react-dom +
 * the assistant-ui packages all pin `?deps=react@19.3.1` so the whole graph
 * shares ONE React instance. Styling is inline (Tailwind/className from the
 * host do not reach the shadow root) on the skin tokens (`var(--sheet)`,
 * `var(--ink)`, …), which the shadow root inherits from the host's :root, so
 * a skin change repaints without a re-render. The one style sheet passed to
 * api.injectCss covers what inline styles cannot express (keyframes,
 * ::placeholder, :hover, :focus-within).
 *
 * This renderer supplies its own raw mount lifecycle and context; it shares
 * the runtime's exact React/DOM graph. The exported renderer marker and
 * component satisfy the handler's static default-export shape contract.
 */
// Every esm.sh import in the @assistant-ui graph ALSO pins the library's
// internal tap/core/store packages and radix-ui. @assistant-ui/react declares
// them with carets, so unpinned esm.sh floats each to its newest release, and
// an internal release can break the store under the pinned build (a 2026-08
// tap release did: "Maximum update depth exceeded" on mount, after which the
// composer discarded every keystroke). The pins are the versions the pinned
// @assistant-ui/react release declares, in one list kept byte for byte here
// and in chat-runtime.ts (P5F03), so the graph holds one instance of each.
// State is read through useAuiState(selector) and actions go through
// useAui() (`aui.composer`), the 0.15 API. `python -m collimate._host.module_mirror
// bump` moves the family and the versions inside these lists together; after
// a bump, check the pins against the new release's declared ranges and verify
// chat typing end-to-end. The SDK's module snapshot freezes what these lists
// resolve (module-snapshot/, which the app serves before upstream).
import React, { useEffect, useMemo, useRef, useState } from "https://esm.sh/react@19.3.1?target=es2022";
import { createRoot } from "https://esm.sh/react-dom@19.3.1/client?deps=react@19.3.1&target=es2022";
import {
  ActionBarPrimitive,
  AssistantRuntimeProvider,
  AuiIf,
  ComposerPrimitive,
  ErrorPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
  useMessagePartReasoning,
  useMessagePartSource,
  useMessagePartText,
} from "https://esm.sh/@assistant-ui/react@0.15.31?deps=react@19.3.1,react-dom@19.3.1,@assistant-ui/tap@0.9.24,@assistant-ui/core@0.3.31,@assistant-ui/store@0.3.22,radix-ui@1.8.1&target=es2022";
// Markdown rendering — MarkdownTextPrimitive with an inline-styled `components`
// override map (no Tailwind; shadow-DOM safe). The deps pin MUST carry the same
// @assistant-ui/react so esm.sh reuses one core/store instance.
import { MarkdownTextPrimitive } from "https://esm.sh/@assistant-ui/react-markdown@0.14.24?deps=react@19.3.1,react-dom@19.3.1,@assistant-ui/tap@0.9.24,@assistant-ui/core@0.3.31,@assistant-ui/store@0.3.22,radix-ui@1.8.1,@assistant-ui/react@0.15.31&target=es2022";
import {
  ArrowDown, ArrowUp, Brain, Check,
  Copy, CornerUpRight, File, FileAudio, FileText, FileVideo, Globe, ListChecks, Loader,
  Paperclip, Pencil,
  Repeat, Search, Square, Terminal, Trash2, Wrench, X,
} from "https://esm.sh/lucide-react@1.61.0?deps=react@19.3.1&target=es2022&exports=ArrowDown,ArrowUp,Brain,Check,Copy,CornerUpRight,File,FileAudio,FileText,FileVideo,Globe,ListChecks,Loader,Paperclip,Pencil,Repeat,Search,Square,Terminal,Trash2,Wrench,X";

// ── theme ─────────────────────────────────────────────────────────────
//
// Every colour and font is a skin token. The shadow root inherits the host's
// :root variables (and its color-scheme), so the chat follows the skin with no
// theme state of its own: the ground is the sheet, bubbles and panels are
// sheet-2, teal marks live and on-states, red is destructive, green is pass.

export const T = {
  sheet: "var(--sheet)",
  sheet2: "var(--sheet-2)",
  sheet3: "var(--sheet-3)",
  line: "var(--line)",
  line2: "var(--line-2)",
  ink: "var(--ink)",
  ink2: "var(--ink-2)",
  ink3: "var(--ink-3)",
  teal: "var(--teal)",
  red: "var(--red)",
  redSoft: "var(--red-soft)",
  green: "var(--green)",
  codeBg: "var(--code-bg)",
  shadow: "var(--shadow)",
} as const;

export const FONT_UI = "var(--font-ui)";
export const MONO = "var(--font-mono)";

/** The footer's connection dot: green while the socket is open, a hint while
 *  the run starts or the socket connects, red once it is closed or held by
 *  another view. */
export function statusDotColor(status: string): string {
  if (status === "open") return T.green;
  if (status === "connecting" || status === "starting") return T.ink3;
  return T.red;
}

/** The note under a queued bubble: it goes out when the chat is live. */
export const QUEUED_NOTE = "Queued — sent when the chat is live";

/** What a pointer that does not parse paints, on the same tokens. */
export const INVALID_SESSION_HTML =
  `<div style="color:${T.ink2};background:${T.sheet};padding:12px;` +
  `font-family:${FONT_UI};font-size:14px;">Not a valid .chat session.</div>`;

const THREAD_MAX_WIDTH = 704; // 44rem

// What inline styles can't express: keyframes, pseudo-classes/-elements, and
// the [hidden] attribute assistant-ui's autohiding action bars rely on.
// The box-sizing reset matters: the shadow root has no host CSS, so without
// it every "width: 100%" + padding element (the inline editor, the composer)
// is content-box and overflows its parent by its padding.
export const SHADOW_CSS = `
  *, *::before, *::after { box-sizing: border-box; }
  button, select, input, textarea { font: inherit; }
  @keyframes aui-spin { to { transform: rotate(360deg); } }
  .aui-spin { animation: aui-spin 1s linear infinite; }
  [hidden] { display: none !important; }
  .aui-input::placeholder { color: ${T.ink3}; }
  .aui-composer { transition: border-color .15s; }
  .aui-composer:focus-within { border-color: ${T.line2}; }
  .aui-stb[disabled] { visibility: hidden; }
  .aui-iconbtn { transition: background .12s, color .12s, opacity .12s; }
  .aui-iconbtn:hover:not([disabled]) { background: ${T.sheet2}; color: ${T.ink}; }
  .aui-iconbtn:active:not([disabled]) { background: ${T.sheet3}; }
  .aui-listbtn:hover { background: ${T.sheet2}; }
  .aui-listbtn:active { background: ${T.sheet3}; }
`;

// ── pointer + transcript helpers ──────────────────────────────────────

import {ConnStatus, ThreadEntry, Pointer, ToolLocation, ToolBlock, ToolActivity, Msg, PendingPermission, ConfigOption, normalizePermission, normalizeConfigOptions, ATTACH_DIR, Attachment, UploadStatus, PendingAttachment, guessFromExt, isImageAttachment, sanitizeAttachmentName, dedupeAttachmentName, parseAttachments, parsePointer, parseToolExtras, parseTools, parseTranscript, reconcileTranscript, connStatusOf} from "./message-model";
import {useNativeChat, BUSY_TEXT, STOPPED_TEXT, STARTING_TEXT} from "./chat-runtime";
function chipIconFor(media_type: string, name: string): any {
  const mt = (media_type || guessFromExt(name)).toLowerCase();
  if (mt.startsWith("audio/")) return FileAudio;
  if (mt.startsWith("video/")) return FileVideo;
  if (mt === "application/pdf" || mt.startsWith("text/")) return FileText;
  return File;
}

/** basename, path separators stripped, odd chars folded to "_" (safe cell path). */
// ── shared UI bits ────────────────────────────────────────────────────

const IconBtn = ({ title, style, ...rest }: any) => (
  <button
    type="button"
    className="aui-iconbtn"
    title={title}
    aria-label={title}
    style={{
      display: "inline-flex", alignItems: "center", justifyContent: "center",
      width: 28, height: 28, borderRadius: 8, border: "none", padding: 0,
      background: "transparent", color: T.ink2,
      cursor: "pointer", ...style,
    }}
    {...rest}
  />
);

const smallBtn: React.CSSProperties = {
  border: `1px solid ${T.line2}`, background: "transparent", color: T.ink,
  borderRadius: 8, padding: "4px 12px", fontSize: 13, cursor: "pointer",
};

// ── cell-relative images (markdown `![alt](images/x.png)`) ────────────
//
// Images stored IN the cell are referenced cell-relative (convention:
// `images/<name>`). react-markdown's default <img> resolves such a relative
// src against the app DOCUMENT origin (wrong) so it 404s — rewrite it through
// the host's file-bytes endpoint (the same CAS-backed URL the `image` renderer
// and the Files tab feed straight into <img src>, which serves the bytes with
// the correct Content-Type). ApiCtx threads the NativeRenderer `api` down to
// the markdown component map; react-markdown renders those entries AS
// components, so useContext/useState inside them are valid.
const ApiCtx = React.createContext<any>(null);

/** Already-usable src (leave untouched) vs. a cell-relative path to rewrite. */
function isAbsoluteImageSrc(src: string): boolean {
  return /^(?:https?:|data:|blob:)/i.test(src) || src.startsWith("/");
}

/** Resolve a cell-relative image path through the host's file URL contract. */
function resolveCellImageSrc(src: string, api: any): string {
  const raw = (src || "").trim();
  if (!raw || isAbsoluteImageSrc(raw)) return raw;
  return api.getFileUrl(raw.replace(/^\.\//, ""));
}

/** A cell-stored image, inline-styled for the shadow root (which has no CSS
 *  reset — hence the explicit boxSizing). Degrades to the alt text on error. */
function CellImage({ src, alt, title, api }: { src: string; alt?: string; title?: string; api: any }) {
  const [failed, setFailed] = useState(false);
  const label = alt || "";
  if (failed) {
    return <span style={{ color: T.ink2, fontSize: 12, fontStyle: "italic" }}>🖼 {label || "image not found"}</span>;
  }
  return (
    <img
      src={resolveCellImageSrc(src, api)}
      alt={label}
      title={title || label || undefined}
      loading="lazy"
      onError={() => setFailed(true)}
      style={{ display: "block", maxWidth: "100%", height: "auto", margin: "8px 0",
        borderRadius: 8, border: `1px solid ${T.line}`, boxSizing: "border-box" }}
    />
  );
}

// ── message parts (markdown, reasoning, sources, tools) ──────────────

/** Markdown link: a cell-relative href (the common case — the agent links
 *  files it wrote, `[notes.md](notes.md)`) opens as that
 *  file's renderer tab via api.revealFile — navigating the browser to a
 *  relative URL would just break out of the SPA. External/absolute hrefs
 *  keep the new-browser-tab behavior. Not spread with {...p} — that would
 *  forward react-markdown's `node` onto the DOM element. */
const CellLink = (p: any) => {
  const api = React.useContext(ApiCtx);
  const href: string = p.href || "";
  const external = /^(https?:|mailto:|#|\/)/i.test(href);
  if (external) {
    return (
      <a style={{ color: T.teal }} href={href} title={p.title}
        target="_blank" rel="noopener noreferrer">{p.children}</a>
    );
  }
  const rel = (() => {
    const raw = href.replace(/^\.\//, "");
    try { return raw.split("/").map(decodeURIComponent).join("/"); } catch { return raw; }
  })();
  return (
    <a style={{ color: T.teal, cursor: "pointer" }} href={href}
      title={p.title || rel}
      onClick={(e) => {
        e.preventDefault();
        try { api?.revealFile?.(rel); } catch { /* noop — dead link */ }
      }}>{p.children}</a>
  );
};

// Markdown element overrides — inline-styled so they render correctly in the
// shadow root (react-markdown emits no default classes).
const md = {
  p: (p: any) => <p style={{ margin: "0 0 8px" }} {...p} />,
  a: (p: any) => <CellLink href={p.href} title={p.title}>{p.children}</CellLink>,
  ul: (p: any) => <ul style={{ margin: "0 0 8px", paddingLeft: 20 }} {...p} />,
  ol: (p: any) => <ol style={{ margin: "0 0 8px", paddingLeft: 20 }} {...p} />,
  li: (p: any) => <li style={{ margin: "2px 0" }} {...p} />,
  h1: (p: any) => <h1 style={{ fontSize: 20, margin: "10px 0 6px" }} {...p} />,
  h2: (p: any) => <h2 style={{ fontSize: 17, margin: "10px 0 6px" }} {...p} />,
  h3: (p: any) => <h3 style={{ fontSize: 15, margin: "8px 0 4px" }} {...p} />,
  blockquote: (p: any) => <blockquote style={{ margin: "0 0 8px", padding: "2px 10px", borderLeft: `2px solid ${T.line2}`, color: T.ink2 }} {...p} />,
  hr: () => <hr style={{ border: "none", borderTop: `1px solid ${T.line}`, margin: "10px 0" }} />,
  pre: (p: any) => <pre style={{ background: T.codeBg, border: `1px solid ${T.line}`, padding: 10, borderRadius: 10, overflow: "auto", margin: "0 0 8px" }} {...p} />,
  code: (p: any) => <code style={{ background: T.codeBg, padding: "1px 4px", borderRadius: 4, fontFamily: MONO, fontSize: "0.92em" }} {...p} />,
  table: (p: any) => <table style={{ borderCollapse: "collapse", margin: "0 0 8px" }} {...p} />,
  th: (p: any) => <th style={{ border: `1px solid ${T.line}`, padding: "3px 8px", textAlign: "left" }} {...p} />,
  td: (p: any) => <td style={{ border: `1px solid ${T.line}`, padding: "3px 8px" }} {...p} />,
  // Cell-stored images (`![alt](images/x.png)`) rewritten through the file
  // endpoint; absolute / data: / root-relative srcs pass through untouched. Not
  // spread with {...p} — that would also forward react-markdown's `node` onto
  // the DOM <img>. api comes from ApiCtx (rendered as a component → hook is ok).
  img: (p: any) => <CellImage src={p.src} alt={p.alt} title={p.title} api={React.useContext(ApiCtx)} />,
};
const MarkdownText = () => <MarkdownTextPrimitive smooth components={md} />;

// User (and system) bubbles render as PLAIN TEXT — no markdown — EXCEPT inline
// image refs (`![alt](src)`), which resolve through the same cell file endpoint
// as assistant images so a pasted `![](images/x.png)` shows the picture instead
// of raw markdown. ONLY image syntax is interpreted: the text is split around
// each match and the rest kept literal (pre-wrap), so bold/headings/lists/etc.
// stay verbatim and the existing plain-text rendering is otherwise untouched.
// (Assistant replies get FULL markdown via the `md` map above; this is the
// deliberately-minimal, low-regression user-side counterpart.)
const UserText = () => {
  const part: any = useMessagePartText();
  const api = React.useContext(ApiCtx);
  const text: string = part?.text ?? "";
  if (!text || text.indexOf("![") < 0) {
    return <span style={{ whiteSpace: "pre-wrap" }}>{text}</span>;   // fast path: unchanged
  }
  const re = /!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;   // ![alt](src "optional title")
  const nodes: React.ReactNode[] = [];
  let last = 0, k = 0, m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      nodes.push(<span key={`t${k}`} style={{ whiteSpace: "pre-wrap" }}>{text.slice(last, m.index)}</span>);
    }
    nodes.push(<CellImage key={`i${k}`} src={m[2]} alt={m[1]} api={api} />);
    last = m.index + m[0].length;
    k++;
  }
  if (last < text.length) {
    nodes.push(<span key={`t${k}`} style={{ whiteSpace: "pre-wrap" }}>{text.slice(last)}</span>);
  }
  return <>{nodes}</>;
};

// Reasoning + Sources message parts. Defensive about the exact part shape so a mismatch renders
// nothing rather than crashing the thread.
//
// Thought sections: while a turn streams, the native reducer splits the
// thinking at tool/text boundaries (Msg.thinkingSections, live-only — read
// through the same by-id message metadata as AttachmentsRow). Multiple
// sections render as one collapsible block per section; the durable
// transcript keeps one string, so a reloaded turn shows a single section.
const Reasoning = () => {
  const part: any = useMessagePartReasoning();
  const id = useAuiState((s: any) => s.message.id) as string;
  const display = React.useContext(MessageDisplayCtx);
  const text = part?.text ?? "";
  if (!text) return null;
  const sections = display?.byId.get(id)?.thinkingSections;
  // Each block is shown trimmed: a streamed chunk boundary (or the model
  // itself) often opens/closes a thought with newlines, which pre-wrap would
  // paint as blank lines inside the collapsible.
  const blocks = (sections && sections.length > 1 ? sections : [text])
    .map((b) => (b || "").trim()).filter(Boolean);
  if (!blocks.length) return null;
  return (
    <>
      {blocks.map((b, i) => (
        <details key={i} style={{ margin: "0 0 8px", color: T.ink2, fontSize: 13 }}>
          <summary style={{ cursor: "pointer" }}>
            {blocks.length > 1 ? `Reasoning · ${i + 1}/${blocks.length}` : "Reasoning"}
          </summary>
          <div style={{ whiteSpace: "pre-wrap", paddingTop: 4, fontStyle: "italic" }}>{b}</div>
        </details>
      ))}
    </>
  );
};
const SourcePart = () => {
  const part: any = useMessagePartSource();
  const url = part?.url;
  if (!url) return null;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer"
      style={{ display: "inline-block", margin: "2px 6px 2px 0", padding: "1px 8px",
        border: `1px solid ${T.line}`, borderRadius: 999, color: T.teal,
        fontSize: 12, textDecoration: "none" }}>🔗 {part?.title || url}</a>
  );
};

// ── kind-typed tool display (agent-v2) ───────────────────────────────
//
// The ACP normalizer types each tool call with a ToolKind and may attach
// file locations and structured diff/terminal content. The lucide icon per
// kind replaces the generic 🔧; unknown/absent kinds keep the wrench.
const TOOL_KIND_ICONS: Record<string, any> = {
  read: FileText, edit: Pencil, delete: Trash2, move: CornerUpRight,
  search: Search, execute: Terminal, think: Brain, fetch: Globe,
  switch_mode: Repeat, other: Wrench,
};

type DiffRow = { t: "same" | "add" | "del"; line: string };

/** Bounded line diff for ACP diff blocks: plain LCS on lines, with a
 *  whole-replace fallback past the quadratic-comfort cap. Good enough for a
 *  chat bubble — the Files tab owns the real diff view. */
function diffLines(oldText: string, newText: string): DiffRow[] {
  const a = oldText ? oldText.split("\n") : [];
  const b = newText ? newText.split("\n") : [];
  const CAP = 300;
  if (a.length > CAP || b.length > CAP) {
    return [
      ...a.slice(0, CAP).map((line): DiffRow => ({ t: "del", line })),
      ...b.slice(0, CAP).map((line): DiffRow => ({ t: "add", line })),
    ];
  }
  const n = a.length, m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out: DiffRow[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ t: "same", line: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ t: "del", line: a[i] }); i++; }
    else { out.push({ t: "add", line: b[j] }); j++; }
  }
  while (i < n) out.push({ t: "del", line: a[i++] });
  while (j < m) out.push({ t: "add", line: b[j++] });
  return out;
}

/** Collapse long unchanged runs (keep 2 context lines each side). */
function collapseSameRuns(rows: DiffRow[]): (DiffRow | { t: "gap"; count: number })[] {
  const out: (DiffRow | { t: "gap"; count: number })[] = [];
  let run: DiffRow[] = [];
  const flush = (last: boolean) => {
    const keepHead = out.length ? 2 : 0;         // no leading context at the top
    const keepTail = last ? 0 : 2;
    if (run.length > keepHead + keepTail + 3) {
      out.push(...run.slice(0, keepHead));
      out.push({ t: "gap", count: run.length - keepHead - keepTail });
      out.push(...run.slice(run.length - keepTail));
    } else {
      out.push(...run);
    }
    run = [];
  };
  for (const r of rows) {
    if (r.t === "same") { run.push(r); continue; }
    if (run.length) flush(false);
    out.push(r);
  }
  if (run.length) flush(true);
  return out;
}

/** One ACP diff block: path header + tinted add/del rows. */
function DiffBlockImpl({ block }: { block: Extract<ToolBlock, { type: "diff" }> }) {
  const rows = useMemo(
    () => collapseSameRuns(diffLines(block.oldText || "", block.newText || "")),
    [block.oldText, block.newText]);
  return (
    <div style={{ margin: "6px 0 0", border: `1px solid ${T.line}`, borderRadius: 8, overflow: "hidden" }}>
      {block.path ? (
        <div style={{ padding: "4px 8px", fontFamily: MONO, fontSize: 11, color: T.ink2,
          background: T.sheet2, borderBottom: `1px solid ${T.line}` }}>{block.path}</div>
      ) : null}
      <pre style={{ margin: 0, padding: "4px 0", overflow: "auto", maxHeight: 320,
        fontFamily: MONO, fontSize: 12, lineHeight: 1.45 }}>
        {rows.map((r, k) => r.t === "gap" ? (
          <div key={k} style={{ padding: "0 8px", color: T.ink2, fontStyle: "italic" }}>
            ⋯ {r.count} unchanged line{r.count === 1 ? "" : "s"}
          </div>
        ) : (
          <div key={k} style={{ padding: "0 8px", whiteSpace: "pre-wrap", overflowWrap: "anywhere",
            ...(r.t === "add" ? { background: `color-mix(in srgb, ${T.green} 14%, transparent)` }
              : r.t === "del" ? { background: T.redSoft, opacity: 0.85 } : {}) }}>
            <span style={{ display: "inline-block", width: 14, color: T.ink2, userSelect: "none" }}>
              {r.t === "add" ? "+" : r.t === "del" ? "−" : " "}
            </span>
            {r.line}
          </div>
        ))}
      </pre>
    </div>
  );
}
// Memoized on the diff's content: ToolFallback re-renders on every stream
// frame (message metadata changes with the messages) and hands in a fresh block
// object, so identity alone would rebuild every row each frame.
const DiffBlock = React.memo(DiffBlockImpl, (a, b) =>
  a.block.path === b.block.path && a.block.oldText === b.block.oldText
  && a.block.newText === b.block.newText);

// Tool-call rendering uses the native reducer's tool frames. Its
// typed extras — kind, locations, diff/terminal content — are looked up by
// toolCallId through the same by-id message metadata as AttachmentsRow.
const ToolFallback = (p: any) => {
  const display = React.useContext(MessageDisplayCtx);
  const api = React.useContext(ApiCtx);
  const name = p?.toolName || p?.part?.toolName || "tool";
  const callId = String(p?.toolCallId || "");
  const meta = callId ? display?.toolById.get(callId) : undefined;
  const args = p?.argsText ?? (p?.args ? JSON.stringify(p.args, null, 2) : "");
  const result = typeof p?.result === "string" ? p.result
    : p?.result !== undefined ? JSON.stringify(p.result, null, 2) : "";
  // Running when the tool hasn't produced a result yet. assistant-ui models a
  // tool part's lifecycle in `status.type` (running | requires-action |
  // complete | …); the native signal is the tool record's own status.
  const running = meta
    ? meta.status === "running"
    : p?.status?.type
      ? p.status.type === "running" || p.status.type === "requires-action"
      : !("result" in p);
  const failed = meta?.status === "error";
  const incomplete = meta?.status === "incomplete";
  const KindIcon = (meta?.kind && TOOL_KIND_ICONS[meta.kind]) || Wrench;
  const diffs = (meta?.content || []).filter((b): b is Extract<ToolBlock, { type: "diff" }> => b.type === "diff");
  const terminals = (meta?.content || []).filter((b): b is Extract<ToolBlock, { type: "terminal" }> => b.type === "terminal");
  return (
    <details style={{ margin: "0 0 8px", border: `1px solid ${T.line}`, borderRadius: 10, padding: "6px 10px", fontSize: 13 }}
      {...(diffs.length ? { open: true } : {})}>
      <summary style={{ cursor: "pointer", color: failed ? T.red : T.ink2 }}>
        {running
          ? <Loader size={13} className="aui-spin" style={{ verticalAlign: "-2px", marginRight: 6 }} />
          : <KindIcon size={13} style={{ verticalAlign: "-2px", marginRight: 6 }} />}
        {name}
        {failed ? <span style={{ marginLeft: 6, fontSize: 11 }}>failed</span> : null}
        {incomplete ? <span style={{ marginLeft: 6, fontSize: 11 }}>incomplete</span> : null}
      </summary>
      {meta?.locations?.length ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, margin: "6px 0 0" }}>
          {meta.locations.map((loc, k) => (
            <button key={k} type="button" title={`Open ${loc.path}`}
              onClick={() => { try { api?.revealFile?.(loc.path); } catch { /* noop */ } }}
              style={{ border: `1px solid ${T.line2}`, borderRadius: 999, padding: "1px 8px",
                background: "transparent", color: T.ink2, fontFamily: MONO, fontSize: 11,
                cursor: "pointer" }}>
              {loc.path}{typeof loc.line === "number" ? `:${loc.line}` : ""}
            </button>
          ))}
        </div>
      ) : null}
      {args ? <pre style={{ margin: "6px 0 0", overflow: "auto", fontFamily: MONO, fontSize: 12, color: T.ink2 }}>{args}</pre> : null}
      {diffs.map((b, k) => <DiffBlock key={k} block={b} />)}
      {terminals.map((b, k) => (
        <div key={k} style={{ display: "flex", alignItems: "center", gap: 6, margin: "6px 0 0",
          color: T.ink2, fontSize: 12 }}>
          <Terminal size={13} /> terminal {b.terminalId}
        </div>
      ))}
      {result && !diffs.length
        ? <pre style={{ margin: "6px 0 0", overflow: "auto", fontFamily: MONO, fontSize: 12 }}>{result}</pre>
        : null}
    </details>
  );
};

const assistantParts = {
  Text: MarkdownText,
  Reasoning,
  Source: SourcePart,
  tools: { Fallback: ToolFallback },
};

// ── message display metadata ──────────────────────────────────────────
// The assistant-ui message parts carry text and tool IDs. Native transcript
// metadata supplies attachments, reasoning sections, typed tools, and usage.
interface MessageDisplay {
  byId: Map<string, Msg>;
  toolById: Map<string, ToolActivity>;
}
const MessageDisplayCtx = React.createContext<MessageDisplay | null>(null);

// ── attachment rendering (per-message) ───────────────────────────────
//
// A message's attachments render UNDER its text: images inline (reusing
// CellImage, the same cell-file resolver as markdown images) and everything
// else as a compact chip that opens the file in the cell's Files tab
// (api.revealFile). The attachments are looked up from the native runtime's
// Msg list by id, so no custom assistant-ui content-part type is needed and
// both user and assistant messages render identically.

function FileChip({ att, api }: { att: Attachment; api: any }) {
  const Icon = chipIconFor(att.media_type, att.name);
  const ext = (att.name.split(".").pop() || "").toUpperCase();
  return (
    <button type="button" title={`Open ${att.name}`}
      onClick={() => { try { api?.revealFile?.(att.path); } catch { /* noop */ } }}
      style={{ display: "inline-flex", alignItems: "center", gap: 8, maxWidth: 260,
        border: `1px solid ${T.line}`, borderRadius: 10, padding: "6px 10px",
        background: T.sheet2, color: T.ink, cursor: "pointer", font: "inherit", fontSize: 13 }}>
      <Icon size={16} style={{ color: T.ink2, flex: "0 0 auto" }} />
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>{att.name}</span>
      {ext && ext.length <= 5 ? <span style={{ color: T.ink2, fontSize: 11, flex: "0 0 auto" }}>{ext}</span> : null}
    </button>
  );
}

function AttachmentsRow({ id }: { id: string }) {
  const display = React.useContext(MessageDisplayCtx);
  const api = React.useContext(ApiCtx);
  const atts = display?.byId.get(id)?.attachments;
  if (!atts || !atts.length) return null;
  const images = atts.filter(isImageAttachment);
  const files = atts.filter((a) => !isImageAttachment(a));
  return (
    <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 6 }}>
      {images.map((a, i) => <CellImage key={`img${i}`} src={a.path} alt={a.name} api={api} />)}
      {files.length ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {files.map((a, i) => <FileChip key={`f${i}`} att={a} api={api} />)}
        </div>
      ) : null}
    </div>
  );
}

// ── assistant meta (stopped marker + usage footer) ───────────────────
//
// Display-only, read from the native runtime's Msg by id.

/** Compact token count: 1200 → "1.2k", 340 → "340". */
function formatCount(n: number): string {
  if (!isFinite(n)) return "";
  if (Math.abs(n) >= 1000) {
    const k = n / 1000;
    return `${Math.abs(k) >= 100 ? Math.round(k) : k.toFixed(1).replace(/\.0$/, "")}k`;
  }
  return String(Math.round(n));
}

function AssistantMeta({ id }: { id: string }) {
  const display = React.useContext(MessageDisplayCtx);
  const m = display?.byId.get(id);
  if (!m) return null;
  const u = m.usage;
  const bits: string[] = [];
  if (u) {
    if (typeof u.input_tokens === "number") bits.push(`${formatCount(u.input_tokens)} in`);
    if (typeof u.output_tokens === "number") bits.push(`${formatCount(u.output_tokens)} out`);
    if (typeof u.total_cost_usd === "number") {
      bits.push(`$${u.total_cost_usd.toFixed(u.total_cost_usd >= 1 ? 2 : 3)}`);
    }
  }
  if (!m.stopped && !bits.length) return null;
  return (
    <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 10,
      marginTop: 4, color: T.ink2, fontSize: 12 }}>
      {m.stopped ? <span style={{ fontStyle: "italic" }}>Stopped</span> : null}
      {bits.length ? <span>{bits.join(" · ")}</span> : null}
    </div>
  );
}

// ── ACP session surfaces: the live plan card + the context meter ─────

/** Status glyph for a plan entry (ACP PlanEntryStatus). */
function planGlyph(status: string): { glyph: string; color: string; done: boolean } {
  if (status === "completed") return { glyph: "✓", color: T.green, done: true };
  if (status === "in_progress") return { glyph: "◐", color: T.teal, done: false };
  return { glyph: "○", color: T.ink2, done: false };
}

/** The agent's live plan (ACP `plan` updates — wholesale replace). Collapsed
 *  by default once everything is done; open while work is in flight. */
const PlanCard = React.memo(function PlanCard({ entries }: { entries: { content: string; priority: string; status: string }[] }) {
  const done = entries.filter((e) => e.status === "completed").length;
  const allDone = done === entries.length;
  return (
    <details open={!allDone} style={{ border: `1px solid ${T.line}`, borderRadius: 10,
      padding: "6px 12px", fontSize: 13, background: T.sheet2 }}>
      <summary style={{ cursor: "pointer", color: T.ink2, display: "flex",
        alignItems: "center", gap: 6 }}>
        <ListChecks size={14} style={{ flex: "0 0 auto" }} />
        <span style={{ fontWeight: 500 }}>Plan</span>
        <span style={{ fontSize: 12 }}>{done}/{entries.length}</span>
      </summary>
      <div style={{ display: "flex", flexDirection: "column", gap: 3, padding: "6px 0 2px",
        maxHeight: 180, overflowY: "auto" }}>
        {entries.map((e, k) => {
          const g = planGlyph(e.status);
          return (
            <div key={k} style={{ display: "flex", gap: 8, alignItems: "baseline", minWidth: 0 }}>
              <span style={{ color: g.color, flex: "0 0 auto", fontSize: 12 }}>{g.glyph}</span>
              <span style={{ minWidth: 0, overflowWrap: "break-word",
                color: g.done ? T.ink2 : T.ink,
                ...(g.done ? { textDecoration: "line-through" } : {}) }}>
                {e.content}
              </span>
              {e.priority === "high" && !g.done
                ? <span style={{ color: T.red, fontSize: 11, flex: "0 0 auto" }}>high</span>
                : null}
            </div>
          );
        })}
      </div>
    </details>
  );
});

/** The ACP context meter — window occupancy + cumulative session cost (from
 *  `usage_update`; NOT per-turn tokens, which have no ACP source). */
const ContextMeter = React.memo(function ContextMeter({ ctx }: { ctx: { used?: number; size?: number; cost?: { amount: number; currency: string } } }) {
  const bits: React.ReactNode[] = [];
  if (typeof ctx.used === "number" && typeof ctx.size === "number" && ctx.size > 0) {
    const pct = Math.min(100, Math.max(0, (ctx.used / ctx.size) * 100));
    bits.push(
      <span key="ctx" title={`${formatCount(ctx.used)} of ${formatCount(ctx.size)} context tokens used`}
        style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
        <span style={{ display: "inline-block", width: 56, height: 4, borderRadius: 2,
          background: T.sheet2, overflow: "hidden" }}>
          <span style={{ display: "block", width: `${pct}%`, height: "100%",
            background: pct > 85 ? T.red : T.teal }} />
        </span>
        context {Math.round(pct)}%
      </span>,
    );
  } else if (typeof ctx.used === "number") {
    bits.push(<span key="ctx">context {formatCount(ctx.used)}</span>);
  }
  if (ctx.cost && typeof ctx.cost.amount === "number") {
    const cur = ctx.cost.currency && ctx.cost.currency.toUpperCase() !== "USD"
      ? ` ${ctx.cost.currency}` : "";
    bits.push(
      <span key="cost" title="Cumulative session cost">
        ${ctx.cost.amount.toFixed(ctx.cost.amount >= 1 ? 2 : 3)}{cur}
      </span>,
    );
  }
  if (!bits.length) return null;
  return (
    <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center",
      gap: 12, color: T.ink2, fontSize: 11, padding: "0 4px" }}>
      {bits}
    </div>
  );
});

// The reserved config-option categories, in display order; unknown
// categories sort after them (UX metadata only, per the spec).
const CONFIG_CATEGORY_ORDER: Record<string, number> = {
  mode: 0, model: 1, model_config: 2, thought_level: 3,
};

/** The generic configOptions row (agent-v2 §2.1 Phase 4): one compact
 *  labelled select per agent-exposed option — mode, model, thought_level,
 *  whatever the agent offers — set mid-session over the surface WS. The
 *  option being applied disables until the agent's confirming refresh. */
const ConfigOptionsRow = React.memo(function ConfigOptionsRow({ options, pending, disabled, onSet }: {
  options: ConfigOption[]; pending: string; disabled?: boolean;
  onSet: (id: string, value: string) => void;
}) {
  const sorted = useMemo(() => [...options].sort((a, b) =>
    (CONFIG_CATEGORY_ORDER[a.category] ?? 9) - (CONFIG_CATEGORY_ORDER[b.category] ?? 9)), [options]);
  if (!options.length) return null;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center",
      gap: 8, padding: "0 4px" }}>
      {sorted.map((o) => (
        <label key={o.id} title={o.description || o.name}
          style={{ display: "inline-flex", alignItems: "center", gap: 4,
            color: T.ink2, fontSize: 11 }}>
          {o.name}
          <select
            value={o.currentValue}
            disabled={disabled || pending === o.id}
            onChange={(e) => {
              const v = (e.target as HTMLSelectElement).value;
              if (v !== o.currentValue) onSet(o.id, v);
            }}
            style={{ maxWidth: 180, border: `1px solid ${T.line2}`,
              background: T.sheet, color: T.ink, borderRadius: 6,
              padding: "2px 4px", fontSize: 11,
              opacity: (disabled || pending === o.id) ? 0.6 : 1 }}>
            {/* A current value outside the offered list (a spawn-profile
                model the agent doesn't enumerate) still displays. */}
            {o.currentValue && !o.options.some((v) => v.value === o.currentValue) ? (
              <option value={o.currentValue}>{o.currentValue}</option>
            ) : null}
            {o.options.map((v) => (
              <option key={v.value} value={v.value} title={v.description || ""}>
                {v.name}
              </option>
            ))}
          </select>
        </label>
      ))}
    </div>
  );
});

/** One pending permission card (approvals: ask): the agent-supplied options
 *  render as buttons in the protocol's 4-kind vocabulary — allow_* primary,
 *  reject_* muted — plus a Dismiss that answers cancelled. The generator is
 *  blocked on the answer, so the card sits above the composer until
 *  answered (or its bounded timeout refuses it). */
const PermissionCard = React.memo(function PermissionCard({ req, onAnswer }: {
  req: PendingPermission;
  onAnswer: (id: string, chosen: string | null) => void;
}) {
  const KindIcon = (req.kind && TOOL_KIND_ICONS[req.kind]) || Wrench;
  return (
    <div style={{ border: `1px solid ${T.teal}`, borderRadius: 10,
      padding: "8px 12px", fontSize: 13, background: T.sheet2,
      display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, color: T.ink }}>
        <KindIcon size={14} style={{ color: T.teal, flex: "0 0 auto" }} />
        <span style={{ fontWeight: 600 }}>Approval needed</span>
        <span style={{ color: T.ink2, overflow: "hidden",
          textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>
          {req.title}
        </span>
      </div>
      {req.detail ? (
        <pre style={{ margin: 0, overflow: "auto", maxHeight: 96,
          fontFamily: MONO, fontSize: 12, color: T.ink2 }}>{req.detail}</pre>
      ) : null}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        {req.options.map((o) => {
          const allow = o.kind.startsWith("allow");
          return (
            <button key={o.option_id} type="button"
              onClick={() => onAnswer(req.id, o.option_id)}
              style={{ border: `1px solid ${allow ? T.teal : T.line2}`,
                background: allow ? T.teal : "transparent",
                color: allow ? T.sheet : T.ink,
                borderRadius: 8, padding: "4px 12px", fontSize: 13,
                cursor: "pointer" }}>
              {o.name || o.kind.replace(/_/g, " ")}
            </button>
          );
        })}
        <button type="button" onClick={() => onAnswer(req.id, null)}
          style={{ border: "none", background: "transparent", color: T.ink2,
            fontSize: 12, cursor: "pointer" }}>
          Dismiss (refuse)
        </button>
      </div>
    </div>
  );
});

// ── thread UI ─────────────────────────────────────────────────────────

function ThreadWelcome(props: { label: string }) {
  return (
    <div style={{ margin: "auto", display: "flex", flexDirection: "column",
      justifyContent: "center", width: "100%", maxWidth: THREAD_MAX_WIDTH,
      flexGrow: 1, padding: "0 24px" }}>
      <div style={{ fontSize: 24, fontWeight: 600, color: T.ink }}>{props.label}</div>
      <div style={{ fontSize: 24, color: T.ink2 }}>How can I help you today?</div>
    </div>
  );
}

function UserMessage() {
  const id = useAuiState((s: any) => s.message.id) as string;
  const display = React.useContext(MessageDisplayCtx);
  const queued = !!display?.byId.get(id)?.queued;
  return (
    <MessagePrimitive.Root
      data-role="user"
      style={{ margin: "0 auto", width: "100%", maxWidth: THREAD_MAX_WIDTH, padding: "10px 8px 0" }}
    >
      <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", minWidth: 0 }}>
        <div style={{ background: T.sheet2, color: T.ink, borderRadius: 16,
          padding: "10px 16px", maxWidth: "85%", overflowWrap: "break-word",
          lineHeight: 1.55, minWidth: 0,
          ...(queued ? { opacity: 0.7, border: `1px dashed ${T.line2}` } : {}) }}>
          <MessagePrimitive.Parts components={{ Text: UserText }} />
          <AttachmentsRow id={id} />
        </div>
        {queued && (
          <div data-testid="chat-queued-note"
            style={{ marginTop: 4, color: T.ink3, fontSize: 11.5 }}>{QUEUED_NOTE}</div>
        )}
      </div>
      <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center",
        gap: 4, minHeight: 28 }}>
        <ActionBarPrimitive.Root hideWhenRunning autohide="always"
          style={{ display: "flex", alignItems: "center", gap: 2 }}>
          <ActionBarPrimitive.Copy asChild>
            <IconBtn title="Copy"><Copy size={14} /></IconBtn>
          </ActionBarPrimitive.Copy>
        </ActionBarPrimitive.Root>
      </div>
    </MessagePrimitive.Root>
  );
}

function SystemMessage() {
  const id = useAuiState((s: any) => s.message.id) as string;
  return (
    <MessagePrimitive.Root
      data-role="system"
      style={{ margin: "0 auto", width: "100%", maxWidth: THREAD_MAX_WIDTH, padding: "10px 8px" }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 6 }}>
        <div style={{ flex: 1, minWidth: 0, border: `1px dashed ${T.line}`, borderRadius: 12,
          padding: "8px 12px", color: T.ink2, fontSize: 13, whiteSpace: "pre-wrap" }}>
          <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 1, marginBottom: 2 }}>system</div>
          <MessagePrimitive.Parts components={{ Text: UserText }} />
          <AttachmentsRow id={id} />
        </div>
      </div>
    </MessagePrimitive.Root>
  );
}

function MessageError() {
  return (
    <MessagePrimitive.Error>
      <ErrorPrimitive.Root style={{ marginTop: 8, borderRadius: 10, padding: 12,
        border: `1px solid ${T.red}`, color: T.red, fontSize: 13,
        background: "color-mix(in srgb, currentColor 8%, transparent)" }}>
        <ErrorPrimitive.Message />
      </ErrorPrimitive.Root>
    </MessagePrimitive.Error>
  );
}

function AssistantMessage() {
  const id = useAuiState((s: any) => s.message.id) as string;
  return (
    <MessagePrimitive.Root
      data-role="assistant"
      style={{ position: "relative", margin: "0 auto", width: "100%",
        maxWidth: THREAD_MAX_WIDTH, padding: "10px 8px" }}
    >
      <div style={{ color: T.ink, lineHeight: 1.6, overflowWrap: "break-word", minWidth: 0 }}>
        <MessagePrimitive.Parts components={assistantParts} />
        <AttachmentsRow id={id} />
        <AssistantMeta id={id} />
        <MessageError />
        <AuiIf condition={(s: any) => s.thread.isRunning
          && !s.message.content.some((p: any) => p.type === "text" && p.text)}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, color: T.ink2 }}>
            <Loader size={15} className="aui-spin" />
            <span style={{ fontSize: 13 }}>Thinking…</span>
          </div>
        </AuiIf>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 4, minHeight: 28, marginTop: 2 }}>
        <ActionBarPrimitive.Root hideWhenRunning autohide="always"
          style={{ display: "flex", alignItems: "center", gap: 2 }}>
          <ActionBarPrimitive.Copy asChild>
            <IconBtn title="Copy">
              <AuiIf condition={(s: any) => s.message.isCopied}><Check size={14} /></AuiIf>
              <AuiIf condition={(s: any) => !s.message.isCopied}><Copy size={14} /></AuiIf>
            </IconBtn>
          </ActionBarPrimitive.Copy>
        </ActionBarPrimitive.Root>
      </div>
    </MessagePrimitive.Root>
  );
}

function ScrollToBottom() {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      <button type="button" className="aui-stb" title="Scroll to bottom"
        style={{ position: "absolute", top: -44, left: "50%", transform: "translateX(-50%)",
          width: 34, height: 34, borderRadius: 999, border: `1px solid ${T.line2}`,
          background: T.sheet, color: T.ink, cursor: "pointer", display: "inline-flex",
          alignItems: "center", justifyContent: "center", boxShadow: T.shadow, zIndex: 10 }}>
        <ArrowDown size={16} />
      </button>
    </ThreadPrimitive.ScrollToBottom>
  );
}

// The paperclip → hidden multi-file input. Resets value after each pick so the
// same file can be chosen again (a `change` fires only on a *different* value).
function AttachButton({ disabled, onFiles }: { disabled: boolean; onFiles: (f: FileList | null) => void }) {
  const ref = useRef<HTMLInputElement | null>(null);
  return (
    <>
      <input ref={ref} type="file" multiple hidden
        onChange={(e) => { onFiles(e.currentTarget.files); e.currentTarget.value = ""; }} />
      <IconBtn title="Attach files" disabled={disabled}
        style={disabled ? { opacity: 0.4, cursor: "default" } : undefined}
        onClick={() => { if (!disabled) ref.current?.click(); }}>
        <Paperclip size={18} />
      </IconBtn>
    </>
  );
}

// A staged (pre-send) attachment: image thumbnail once uploaded, a spinner
// while uploading, else its type icon; red border on failure. × drops it.
function PendingChip({ att, onRemove, api }: { att: PendingAttachment; onRemove: (p: string) => void; api: any }) {
  const err = att.status === "error";
  const uploading = att.status === "uploading";
  const Icon = chipIconFor(att.media_type, att.name);
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, maxWidth: 220,
      border: `1px solid ${err ? T.red : T.line}`, borderRadius: 10,
      padding: "4px 6px 4px 8px", background: T.sheet,
      color: err ? T.red : T.ink, fontSize: 12 }}>
      {isImageAttachment(att) && att.status === "done"
        ? <img src={resolveCellImageSrc(att.path, api)} alt={att.name}
            style={{ width: 28, height: 28, borderRadius: 6, objectFit: "cover",
              border: `1px solid ${T.line}`, flex: "0 0 auto" }} />
        : uploading
          ? <Loader size={14} className="aui-spin" style={{ color: T.ink2, flex: "0 0 auto" }} />
          : <Icon size={14} style={{ color: err ? T.red : T.ink2, flex: "0 0 auto" }} />}
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>{att.name}</span>
      <button type="button" title="Remove attachment" aria-label="Remove attachment"
        onClick={() => onRemove(att.path)}
        style={{ display: "inline-flex", alignItems: "center", border: "none",
          background: "transparent", color: err ? T.red : T.ink2,
          cursor: "pointer", padding: 0, flex: "0 0 auto" }}>
        <X size={14} />
      </button>
    </span>
  );
}

/** Clear the thread composer (its reset is asynchronous; a failure leaves
 *  the text, which the next send overwrites). */
function resetComposer(aui: ReturnType<typeof useAui>): void {
  try { void aui.composer.reset().catch(() => {}); } catch { /* noop */ }
}

// Attach-aware Send (native runtime). ComposerPrimitive.Send is disabled
// whenever the textarea is empty (canSend = !isEmpty && …), which would block an
// attachment-only message — so this replaces it: it stays enabled when there is
// text OR a finished upload, reads the composer text reactively, clears it on
// send, and routes through the same `submit` the Enter key does. Disabled while
// an upload is in flight so a not-yet-written file can't be silently dropped.
function AttachSend({ disabled, uploading, hasReady, submit }: {
  disabled: boolean; uploading: boolean; hasReady: boolean; submit: (t: string) => void;
}) {
  const text: string = (useAuiState((s: any) => s.composer.text) as string) ?? "";
  const aui = useAui();
  const canSend = !disabled && !uploading && (text.trim().length > 0 || hasReady);
  return (
    <button type="button" title="Send message" aria-label="Send message" disabled={!canSend}
      onClick={() => {
        if (!canSend) return;
        const t = text;
        resetComposer(aui);
        submit(t);
      }}
      style={{ width: 32, height: 32, borderRadius: 999, border: "none",
        background: canSend ? T.teal : T.sheet2, color: canSend ? T.sheet : T.ink2,
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        cursor: canSend ? "pointer" : "default" }}>
      <ArrowUp size={16} />
    </button>
  );
}

interface ComposerAttach {
  pending: PendingAttachment[];
  attach: (files: FileList | null) => void;
  remove: (path: string) => void;
  submit: (text: string) => void;
}

function Composer(props: { disabled: boolean; disabledHint: string; placeholder?: string;
  attach?: ComposerAttach; restoreText?: { text: string; n: number } | null }) {
  const api = React.useContext(ApiCtx);
  const a = props.attach;
  const aui = useAui();
  const uploading = !!a && a.pending.some((p) => p.status === "uploading");
  const hasReady = !!a && a.pending.some((p) => p.status === "done");
  // A refused / uncarried message goes back into the box (see `restoreText`).
  useEffect(() => {
    if (props.restoreText?.text) {
      try { aui.composer.setText(props.restoreText.text); } catch { /* noop */ }
    }
  }, [props.restoreText?.n]);
  // assistant-ui refuses Enter / Send while the thread is running (no queue
  // adapter); the native runtime DOES take a message mid-turn — it queues
  // behind the current reply. Route Enter to our own submit in that state.
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Enter" || e.shiftKey || props.disabled || !a) return;
    // An Enter that confirms an IME candidate is not a send (keyCode 229
    // covers Safari, whose confirming keydown can report isComposing=false).
    if (e.nativeEvent.isComposing || (e.nativeEvent as any).keyCode === 229) return;
    const state = aui.composer.getState();
    if (!state.isEditing) return;
    if (!state.canCancel) return;   // idle: assistant-ui's own send path handles it
    e.preventDefault();
    if (!state.text.trim() && !hasReady) return;
    resetComposer(aui);
    a.submit(state.text);
  };
  return (
    <ComposerPrimitive.Root className="aui-composer"
      style={{ display: "flex", flexDirection: "column", width: "100%",
        border: `1px solid ${T.line}`, borderRadius: 16, background: T.sheet }}>
      {a && a.pending.length ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, padding: "10px 12px 0" }}>
          {a.pending.map((p) => <PendingChip key={p.path} att={p} onRemove={a.remove} api={api} />)}
        </div>
      ) : null}
      <ComposerPrimitive.Input
        className="aui-input"
        placeholder={props.disabled ? props.disabledHint : (props.placeholder || "Send a message…")}
        rows={1}
        autoFocus
        disabled={props.disabled}
        onKeyDown={onKeyDown}
        aria-label="Message input"
        style={{ width: "100%", minHeight: 52, maxHeight: 128, resize: "none",
          background: "transparent", border: "none", outline: "none",
          color: T.ink, font: "inherit", lineHeight: 1.5, padding: "12px 14px 4px" }}
      />
      <div style={{ display: "flex", alignItems: "center", gap: 8,
        justifyContent: a ? "space-between" : "flex-end", padding: "0 8px 8px" }}>
        {a ? <AttachButton disabled={props.disabled} onFiles={a.attach} /> : null}
        <div style={{ display: "flex", alignItems: "center" }}>
          <AuiIf condition={(s: any) => !s.thread.isRunning}>
            {a ? (
              <AttachSend disabled={props.disabled} uploading={uploading} hasReady={hasReady} submit={a.submit} />
            ) : (
              <ComposerPrimitive.Send asChild>
                <button type="button" title="Send message" aria-label="Send message"
                  disabled={props.disabled}
                  style={{ width: 32, height: 32, borderRadius: 999, border: "none",
                    background: props.disabled ? T.sheet2 : T.teal,
                    color: props.disabled ? T.ink2 : T.sheet,
                    display: "inline-flex", alignItems: "center", justifyContent: "center",
                    cursor: props.disabled ? "default" : "pointer" }}>
                  <ArrowUp size={16} />
                </button>
              </ComposerPrimitive.Send>
            )}
          </AuiIf>
          <AuiIf condition={(s: any) => s.thread.isRunning}>
            {/* Mid-turn: Stop, plus Send (the message QUEUES behind the
                current reply — never silently dropped). */}
            {a ? (
              <span style={{ marginRight: 6 }}>
                <AttachSend disabled={props.disabled} uploading={uploading} hasReady={hasReady} submit={a.submit} />
              </span>
            ) : null}
            <ComposerPrimitive.Cancel asChild>
              <button type="button" title="Stop generating" aria-label="Stop generating"
                style={{ width: 32, height: 32, borderRadius: 999, border: "none",
                  background: T.teal, color: T.sheet, display: "inline-flex",
                  alignItems: "center", justifyContent: "center", cursor: "pointer" }}>
                <Square size={12} fill="currentColor" />
              </button>
            </ComposerPrimitive.Cancel>
          </AuiIf>
        </div>
      </div>
    </ComposerPrimitive.Root>
  );
}


function ChatUI(props: {
  label: string; status: string;
  banner?: string; bannerKind?: "error" | "info"; onDismissBanner?: () => void;
  restoreText?: { text: string; n: number } | null;
  composerDisabled: boolean; composerHint: string;
  composerPlaceholder?: string;
  attach?: ComposerAttach;
  // ACP session surfaces (native runtime only): the live plan + the context
  // meter (window occupancy + cumulative session cost).
  plan?: { content: string; priority: string; status: string }[];
  context?: { used?: number; size?: number; cost?: { amount: number; currency: string } } | null;
  // approvals: ask — pending permission cards + the answer sender.
  permissionRequests?: PendingPermission[];
  onAnswerPermission?: (id: string, chosen: string | null) => void;
  // The generic configOptions UI (agent-v2 §2.1 Phase 4).
  configOptions?: ConfigOption[];
  configPending?: string;
  onSetConfigOption?: (id: string, value: string) => void;
}) {
  // Single conversation — no thread sidebar. The connection dot (formerly
  // the sidebar's) rides the footer next to the context meter.
  const dot = statusDotColor(props.status);
  const statusTitle = props.status === "open" ? "connected"
    : props.status === "starting" ? "starting…"
    : props.status === "connecting" ? "connecting…"
    : props.status === "busy" ? "open in another view" : "stopped";
  return (
    <div style={{ display: "flex", flexDirection: "row", height: "100%", minHeight: 0,
      background: T.sheet, color: T.ink,
      fontFamily: FONT_UI, fontSize: 14 }}>
      <ThreadPrimitive.Root style={{ flex: 1, minWidth: 0, display: "flex",
        flexDirection: "column", minHeight: 0 }}>
        {/* No turnAnchor: anchoring the new turn to the viewport top scrolls
            the prior context out ("only show the new response"). The default
            follow-bottom just reveals new content and yields to user scroll. */}
        <ThreadPrimitive.Viewport
          style={{ position: "relative", flex: 1, display: "flex", flexDirection: "column",
            overflowY: "auto", overflowX: "hidden", scrollBehavior: "smooth",
            padding: "16px 16px 0",
            // Message text must be drag-selectable for copy/paste, whatever
            // selection posture the host page around the shadow root takes.
            userSelect: "text", WebkitUserSelect: "text" }}
        >
          <AuiIf condition={(s: any) => s.thread.isEmpty}>
            <ThreadWelcome label={props.label} />
          </AuiIf>

          <ThreadPrimitive.Messages
            components={{ UserMessage, AssistantMessage, SystemMessage }}
          />

          <ThreadPrimitive.ViewportFooter
            style={{ position: "sticky", bottom: 0, marginTop: "auto",
              marginLeft: "auto", marginRight: "auto", width: "100%",
              maxWidth: THREAD_MAX_WIDTH, display: "flex", flexDirection: "column",
              gap: 8, background: T.sheet, paddingBottom: 12,
              borderRadius: "20px 20px 0 0" }}
          >
            <ScrollToBottom />
            {(props.permissionRequests || []).map((r) => (
              <PermissionCard key={r.id} req={r}
                onAnswer={props.onAnswerPermission || (() => undefined)} />
            ))}
            {props.plan && props.plan.length ? <PlanCard entries={props.plan} /> : null}
            {props.banner ? (
              <div style={{ display: "flex", alignItems: "center", gap: 8,
                border: `1px solid ${props.bannerKind === "info" ? T.line : T.red}`, borderRadius: 10,
                padding: "8px 12px", color: props.bannerKind === "info" ? T.ink2 : T.red, fontSize: 13 }}>
                <span style={{ flex: 1, minWidth: 0, overflowWrap: "break-word" }}>
                  {props.bannerKind === "info" ? "" : "⚠ "}{props.banner}
                </span>
                {props.onDismissBanner && (
                  <button type="button" onClick={props.onDismissBanner}
                    style={{ border: "none", background: "transparent",
                      color: props.bannerKind === "info" ? T.ink2 : T.red,
                      cursor: "pointer", fontSize: 13 }}>dismiss</button>
                )}
              </div>
            ) : null}
            {/* No Reconnect affordance: transient closes self-heal (the
                socket machine retries); a re-serve within the same run
                rewrites this pointer (App remounts onto the new session),
                and a new run opens its own pointer in its own run folder
                (a new tab); this tab's folder is swept when its run ends. A
                "closed" that sticks means the run is STOPPED — the run end
                moved the conversation into the run's log and cleared it
                from the cell (agent-v2 §9.1.8), so what is below is a
                read-only leftover. */}
            {props.status === "closed" && (
              <div style={{ alignSelf: "center", color: T.ink2, fontSize: 12 }}>
                {STOPPED_TEXT}
              </div>
            )}
            {props.status === "busy" && (
              <div style={{ alignSelf: "center", color: T.ink2, fontSize: 12 }}>
                {BUSY_TEXT}
              </div>
            )}
            <Composer disabled={props.composerDisabled} disabledHint={props.composerHint}
              restoreText={props.restoreText}
              placeholder={props.composerPlaceholder} attach={props.attach} />
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span title={statusTitle} style={{ width: 8, height: 8,
                borderRadius: 999, background: dot, flex: "0 0 auto",
                margin: "4px 0 6px 4px" }} />
              {props.configOptions && props.configOptions.length && props.onSetConfigOption ? (
                <ConfigOptionsRow options={props.configOptions}
                  pending={props.configPending || ""}
                  // Off only while the socket is not live.
                  disabled={props.status !== "open"}
                  onSet={props.onSetConfigOption} />
              ) : null}
              {props.context
                ? <div style={{ flex: 1 }}><ContextMeter ctx={props.context} /></div>
                : null}
            </div>
          </ThreadPrimitive.ViewportFooter>
        </ThreadPrimitive.Viewport>
      </ThreadPrimitive.Root>
    </div>
  );
}

// ── app ─────────────────────────────────────────────────────────────

function NativeApp({ api, pointer }: { api: any; pointer: Pointer }) {
  const chat = useNativeChat(api, pointer);
  useEffect(() => { try { api.ready?.(); } catch { /* noop */ } }, [api]);
  const display: MessageDisplay = useMemo(() => {
    const byId = new Map<string, Msg>();
    const toolById = new Map<string, ToolActivity>();
    for (const message of chat.messages) {
      byId.set(message.id, message);
      for (const tool of message.tools || []) if (!toolById.has(tool.id)) toolById.set(tool.id, tool);
    }
    return {byId, toolById};
  }, [chat.messages]);
  return (
    <AssistantRuntimeProvider runtime={chat.runtime}>
      <MessageDisplayCtx.Provider value={display}>
        <ChatUI
          label={pointer.label}
          status={chat.status}
          banner={chat.banner} bannerKind={chat.bannerKind} onDismissBanner={chat.dismissBanner}
          restoreText={chat.restoreText}
          // A starting chat keeps the composer open: what is sent queues for
          // the real session's first confirmed attach.
          composerDisabled={chat.status !== "open" && !chat.queueing}
          composerHint={chat.status === "busy"
            ? BUSY_TEXT
            : chat.status === "connecting"
              ? "Connecting…"
              : STOPPED_TEXT}
          composerPlaceholder={chat.queueing ? STARTING_TEXT
            : chat.status === "connecting" ? "Connecting…" : undefined}
          attach={chat.attachments}
          plan={chat.plan} context={chat.context}
          permissionRequests={chat.permissionRequests}
          onAnswerPermission={chat.answerPermission}
          configOptions={chat.configOptions}
          configPending={chat.configPending}
          onSetConfigOption={chat.setConfigOption}
        />
      </MessageDisplayCtx.Provider>
    </AssistantRuntimeProvider>
  );
}

function App({ api, initialPointer }: { api: any; initialPointer: Pointer }) {
  // Follow pointer rewrites without a remount: a run that registers its chat
  // surface again rewrites its pointer with a fresh session (new
  // chat_id/token), and NativeRenderer deliberately does not remount on
  // file-content changes — so re-read the pointer when files
  // change / content is pushed and remount the runtime tree only when the
  // session identity actually changed.
  const [pointer, setPointer] = useState(initialPointer);
  const pointerRef = useRef(pointer);
  pointerRef.current = pointer;
  const apply = React.useCallback((raw: string) => {
    const next = parsePointer(raw || "");
    if (!next) return;
    const p = pointerRef.current;
    if (next.chat_id !== p.chat_id || next.token !== p.token ||
        next.ws_path !== p.ws_path) {
      setPointer(next);
    }
  }, []);
  // Re-fetch the pointer FILE rather than trusting api.getContent(): on some
  // hosts getContent serves the mount-time cached copy, so a rewritten .chat
  // pointer (fresh chat_id) would never be seen. Falls
  // back to getContent where the sibling read is unavailable.
  // Latest-wins: an older read resolving after a newer one (or after pushed
  // content) must not apply a stale pointer.
  const ptrSeqRef = useRef(0);
  const refreshPointer = React.useCallback(() => {
    (async () => {
      const seq = ++ptrSeqRef.current;
      let raw = "";
      try {
        const name = typeof api.getFilename === "function" ? api.getFilename() : "";
        if (name && typeof api.getSiblingFile === "function") {
          raw = (await api.getSiblingFile(name)) || "";
        }
      } catch { /* noop */ }
      if (!raw) {
        try { raw = (await api.getContent()) || ""; } catch { /* noop */ }
      }
      if (raw && seq === ptrSeqRef.current) apply(raw);
    })();
  }, [api, apply]);
  useEffect(() => {
    const offContent = api.onSetContent?.((c: string) => { ptrSeqRef.current++; apply(c); });
    const offFiles = api.onFilesChange?.((detail: { changedFiles: string[] }) => {
      if (detail.changedFiles.includes(api.getFilename())) refreshPointer();
    });
    return () => {
      try { offContent && offContent(); } catch { /* noop */ }
      try { offFiles && offFiles(); } catch { /* noop */ }
    };
  }, [api, apply, refreshPointer]);
  // The starting placeholder → the real pointer is a new key, so the runtime
  // tree remounts; the messages queued meanwhile live outside it.
  const key = `${pointer.chat_id}:${pointer.token}`;
  // ApiCtx makes `api` reachable from the markdown component map (CellImage /
  // md.img) and UserText, which resolve cell-relative image srcs via
  // api.getFileUrl. Provided once for the runtime tree.
  return (
    <ApiCtx.Provider value={api}>
      <NativeApp key={key} api={api} pointer={pointer} />
    </ApiCtx.Provider>
  );
}

// ── entry ─────────────────────────────────────────────────────────────

async function mount(el: HTMLElement, api: any) {
  let raw = "";
  try { raw = (await api.getContent()) || ""; } catch { /* noop */ }
  const pointer = parsePointer(raw);
  if (!pointer) {
    el.innerHTML = INVALID_SESSION_HTML;
    try { api.ready?.(); } catch { /* noop */ }
    return;
  }
  // Keyframes and pseudo-class rules, injected into the shadow root.
  api.injectCss(SHADOW_CSS);
  const root = createRoot(el);
  root.render(<App api={api} initialPointer={pointer} />);
  // Cleanup so NativeRenderer's unmount (tab-away) tears down the React tree —
  // which fires useNativeChat's effect cleanup and CLOSES the WebSocket, freeing
  // the host session. Without this the socket would linger like a parked iframe.
  return () => { try { root.unmount(); } catch { /* noop */ } };
}

// NativeRenderer mounts via `mount`; the defineRenderer marker + component
// keep the renderer handler's static export-shape check meaningful alongside
// this renderer's own context and lifecycle.
export default { __collimate_renderer__: true, component: App, mount };

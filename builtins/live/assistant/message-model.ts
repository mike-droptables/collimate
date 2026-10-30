export interface ThreadEntry { id: string; title: string; file: string }
export interface Pointer {
  chat_id: string;
  ws_path: string;
  token: string;
  label: string;
  threads_dir: string;
  active_thread: string;
  threads: ThreadEntry[];
  /** The host's starting placeholder (`{"starting": true, "label"}`): the
   *  run is accepted, its chat does not exist yet. The composer stays open
   *  and what is sent queues until the real session's attach carries it. */
  starting: boolean;
}

export type Role = "user" | "assistant" | "system";
// A turn's tool activity — a bounded display record paired to its assistant
// message (the full-fidelity record stays in the trace). `id` is wire-only:
// stream frames pair a completion to its running event by it, and it seeds the
// assistant-ui tool-call part's toolCallId; the durable transcript DROPS it and
// re-synthesizes a stable one on read (see parseTools).
//
// The ACP-typed extras (agent-v2): `kind` is the ToolKind
// (read|edit|delete|move|search|execute|think|fetch|switch_mode|other),
// `locations` the touched files, and `content` the structured blocks — a
// `diff` (rendered inline) or a `terminal` reference. All three persist in
// the durable transcript (collimate/transcript.py widens _clean_tools with
// them) so a reload keeps the typed display.
export interface ToolLocation { path: string; line?: number }
export type ToolBlock =
  | { type: "diff"; path: string; oldText?: string | null; newText: string }
  | { type: "terminal"; terminalId: string };
export interface ToolActivity {
  id: string; name: string; status: string; input?: string; result?: string;
  kind?: string; locations?: ToolLocation[]; content?: ToolBlock[];
}
export interface Msg {
  id: string; role: Role; text: string; thinking?: string;
  // Live-only: the turn's thinking split at tool/text boundaries (thought
  // sections). The durable transcript keeps one string, so this fades to a
  // single section after the turn_end reconcile.
  thinkingSections?: string[];
  tools?: ToolActivity[];               // assistant only — bounded tool record
  usage?: Record<string, number>;       // assistant only — ephemeral per-turn totals (not persisted)
  stopped?: boolean;                    // assistant only — the turn was interrupted (partial content)
  attachments?: Attachment[];
  optimistic?: boolean; // locally sent user message not yet confirmed by the transcript
  queued?: boolean;     // typed before the chat existed; leaves the queue once a confirmed attach carried it
}

// An approval card (gen.ask): options carry `option_id`, the answer sends
// one back, the resolution names the one chosen. `detail` is the text the
// card shows in full (a command line, a path).
export interface PendingPermission {
  id: string; title: string; kind: string; detail?: string;
  options: { option_id: string; name: string; kind: string }[];
}

// One session config option (agent-v2 §2.1 Phase 4: the generic
// configOptions UI). The reserved categories order the row (mode · model ·
// model_config · thought_level, then the rest); unknown categories render
// fine — the spec says categories are UX metadata only, never correctness.
export interface ConfigOption {
  id: string; name: string; category: string; type: string;
  currentValue: string;
  description?: string;
  options: { value: string; name: string; description?: string }[];
}

// One approval card from a `permission_request` frame or a history frame's
// `pending_requests` entry; null when it carries no id.
export function normalizePermission(r: any): PendingPermission | null {
  if (!r || !r.id) return null;
  return {
    id: String(r.id), title: String(r.title || "Permission"),
    kind: String(r.kind || ""),
    ...(r.detail ? { detail: String(r.detail) } : {}),
    options: Array.isArray(r.options)
      ? r.options.filter((o: any) => o && o.option_id).map((o: any) => ({
          option_id: String(o.option_id), name: String(o.name || ""),
          kind: String(o.kind || ""),
        }))
      : [],
  };
}

export function normalizeConfigOptions(raw: any): ConfigOption[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((o: any) => o && typeof o === "object" && o.id != null)
    .map((o: any) => ({
      id: String(o.id), name: String(o.name || o.id),
      category: String(o.category || ""), type: String(o.type || "select"),
      currentValue: o.currentValue != null ? String(o.currentValue) : "",
      ...(o.description ? { description: String(o.description) } : {}),
      options: Array.isArray(o.options)
        ? o.options.filter((v: any) => v && v.value != null).map((v: any) => ({
            value: String(v.value), name: String(v.name || v.value),
            ...(v.description ? { description: String(v.description) } : {}),
          }))
        : [],
    }))
    // Only selects with real choices are actionable; the boolean variant is
    // never offered (the client doesn't advertise configOptions.boolean).
    .filter((o: ConfigOption) => o.type === "select" && o.options.length > 0);
}

// ── file attachments ──────────────────────────────────────────────────
//
// The chat contract carries per-message file attachments: a cell-relative
// { path, name, media_type } where the bytes live under `attachments/<name>`
// in THIS cell. The composer uploads picked files through api.stageDraftFile
// (a draft the generator flushes into the cell — the bytes aren't readable
// back until it does) and stages them as `pending` chips; each rides the next
// sent message. On display an image renders inline (reusing CellImage — the
// same cell-file resolver as markdown images) and everything else as a chip
// that opens the file in the Files tab (api.revealFile), where the matching
// renderer shows it.

export const ATTACH_DIR = "attachments";

export interface Attachment { path: string; name: string; media_type: string }
export type UploadStatus = "uploading" | "done" | "error";
export interface PendingAttachment extends Attachment { status: UploadStatus }

/** Extension → media type, for when the browser leaves File.type empty. */
export function guessFromExt(name: string): string {
  const ext = (String(name || "").split(".").pop() || "").toLowerCase();
  const M: Record<string, string> = {
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif",
    webp: "image/webp", avif: "image/avif", bmp: "image/bmp", svg: "image/svg+xml",
    mp3: "audio/mpeg", wav: "audio/wav", ogg: "audio/ogg", m4a: "audio/mp4",
    flac: "audio/flac", aac: "audio/aac",
    mp4: "video/mp4", webm: "video/webm", mov: "video/quicktime", mkv: "video/x-matroska",
    pdf: "application/pdf", txt: "text/plain", md: "text/markdown",
    csv: "text/csv", json: "application/json", zip: "application/zip",
  };
  return M[ext] || "application/octet-stream";
}

/** True when an attachment should render inline as an image. */
export function isImageAttachment(a: { media_type?: string; name?: string; path?: string }): boolean {
  if ((a.media_type || "").toLowerCase().startsWith("image/")) return true;
  return /\.(png|jpe?g|gif|webp|avif|bmp|svg)$/i.test(a.name || a.path || "");
}

/** The lucide icon for a non-image attachment chip, chosen by media type. */
export function sanitizeAttachmentName(name: string): string {
  const base = String(name || "file").split(/[\\/]/).pop() || "file";
  const clean = base.replace(/[^A-Za-z0-9._ ()\-]+/g, "_").replace(/^\.+/, "").trim();
  return clean || "file";
}

/** Suffix " (2)", " (3)" … before the extension until the name is unused. */
export function dedupeAttachmentName(name: string, taken: Set<string>): string {
  if (!taken.has(name)) return name;
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  let n = 2;
  let candidate = `${stem} (${n})${ext}`;
  while (taken.has(candidate)) { n++; candidate = `${stem} (${n})${ext}`; }
  return candidate;
}

/** Normalize a transcript message's `attachments` array to Attachment[]. */
export function parseAttachments(raw: any): Attachment[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out: Attachment[] = [];
  for (const a of raw) {
    if (!a || typeof a !== "object") continue;
    const path = String(a.path || "").trim();
    if (!path) continue;
    const name = String(a.name || path.slice(path.lastIndexOf("/") + 1) || "file");
    out.push({ path, name, media_type: String(a.media_type || guessFromExt(name)) });
  }
  return out.length ? out : undefined;
}

export function parsePointer(raw: string): Pointer | null {
  try {
    const p = JSON.parse(raw);
    if (!p || typeof p !== "object") return null;
    return {
      chat_id: p.chat_id || "",
      ws_path: p.ws_path || "",
      token: p.token || "",
      label: p.label || "Chat",
      threads_dir: typeof p.threads_dir === "string" ? p.threads_dir : "",
      active_thread: p.active_thread || "main",
      threads: Array.isArray(p.threads) ? p.threads : [],
      starting: p.starting === true,
    };
  } catch {
    return null;
  }
}

/** Normalize a tool entry's ACP-typed extras (kind / locations / content) —
 *  shared by the transcript parser and the wire-frame normalizer so both
 *  produce the same bounded shape. */
export function parseToolExtras(t: any): Pick<ToolActivity, "kind" | "locations" | "content"> {
  const out: Pick<ToolActivity, "kind" | "locations" | "content"> = {};
  if (t?.kind) out.kind = String(t.kind);
  if (Array.isArray(t?.locations)) {
    const locs: ToolLocation[] = [];
    for (const loc of t.locations) {
      if (!loc || typeof loc !== "object") continue;
      const path = String(loc.path || "");
      if (!path) continue;
      locs.push({ path, ...(typeof loc.line === "number" ? { line: loc.line } : {}) });
      if (locs.length >= 20) break;
    }
    if (locs.length) out.locations = locs;
  }
  if (Array.isArray(t?.content)) {
    const blocks: ToolBlock[] = [];
    for (const b of t.content) {
      if (!b || typeof b !== "object") continue;
      if (b.type === "diff") {
        blocks.push({ type: "diff", path: String(b.path || ""),
          ...(b.oldText != null ? { oldText: String(b.oldText) } : {}),
          newText: String(b.newText ?? "") });
      } else if (b.type === "terminal" && b.terminalId) {
        blocks.push({ type: "terminal", terminalId: String(b.terminalId) });
      }
      if (blocks.length >= 8) break;
    }
    if (blocks.length) out.content = blocks;
  }
  return out;
}

/** Normalize an assistant message's `tools` array to ToolActivity[]. The
 *  durable transcript drops the wire id, so synthesize a STABLE one from the
 *  message index `i` and the tool position `k` (`h<i>-t<k>`) — the assistant-ui
 *  tool-call part needs a toolCallId, and stability keeps its part identity
 *  across reconciles. Mirrors the Python writer's _clean_tools (non-empty name
 *  required, status defaults to "done"). */
export function parseTools(raw: any, i: number): ToolActivity[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out: ToolActivity[] = [];
  for (let k = 0; k < raw.length; k++) {
    const t = raw[k];
    if (!t || typeof t !== "object") continue;
    const name = String(t.name ?? "");
    if (!name) continue;
    // The durable record keeps the status a turn ENDED with; a tool still
    // "running" when the turn stopped is incomplete, not live — a reloaded
    // transcript must never spin.
    const status = String(t.status || "done");
    out.push({
      id: `h${i}-t${k}`, name, status: status === "running" ? "incomplete" : status,
      ...(t.input != null && t.input !== "" ? { input: String(t.input) } : {}),
      ...(t.result != null && t.result !== "" ? { result: String(t.result) } : {}),
      ...parseToolExtras(t),
    });
  }
  return out.length ? out : undefined;
}

/** Parse the open transcript ({"messages":[...]}) or a bare array. Ids are
 *  stable for each transcript revision (`h<i>`).
 *  Exported so the parse contract can be exercised under plain Node — see
 *  transcript.test.ts. */
export function parseTranscript(raw: string): Msg[] {
  if (!raw || !raw.trim()) return [];
  let data: any;
  try { data = JSON.parse(raw); } catch { return []; }
  const items = Array.isArray(data) ? data : data?.messages;
  if (!Array.isArray(items)) return [];
  const out: Msg[] = [];
  for (let i = 0; i < items.length; i++) {
    const m = items[i];
    if (!m || typeof m !== "object") continue;
    const role = String(m.role || "").toLowerCase();
    if (role !== "user" && role !== "assistant" && role !== "system") continue;
    out.push({
      id: `h${i}`, role: role as Role, text: String(m.content ?? ""),
      // Thinking is an assistant-message concept — assistant-ui rejects
      // reasoning parts on other roles, so never carry it for them.
      thinking: role === "assistant" ? String(m.thinking ?? "") : "",
      // Tools / stopped are assistant-only too (the writer never emits them for
      // other roles); the transcript's tool ids are wire-only, so parseTools
      // re-synthesizes stable ones from the message + tool position.
      ...(role === "assistant"
        ? { tools: parseTools(m.tools, i), stopped: !!m.stopped } : {}),
      attachments: parseAttachments(m.attachments),
    });
  }
  return out;
}

/** Reconcile a freshly parsed transcript with the list on screen (loadThread's
 *  state update). `live` is the in-flight stream bubble (re-appended last) and
 *  `usage` the last turn's ephemeral usage side-slot.
 *
 *  Id stability: when the reload confirms what's already on screen (same
 *  length, same role+text per index — the common optimistic-append reconcile),
 *  KEEP the existing message ids. Swapping an optimistic id for its h<i>
 *  transcript id at the same position makes assistant-ui's repository record
 *  a phantom sibling branch ("2/2" pickers on untouched messages).
 *
 *  Structural sharing: a message identical to the one already on screen keeps
 *  its object, and an unchanged list returns `prev` itself, so assistant-ui's
 *  per-message conversion cache (keyed by object identity) survives the
 *  frequent files-changed reloads. Exported for transcript.test.ts. */
export function reconcileTranscript(prev: Msg[], parsed: Msg[], live: Msg | undefined,
                                    usage: Record<string, number> | undefined): Msg[] {
  const base = live ? prev.filter((m: Msg) => m.id !== live.id) : prev;
  // Preserve ids along the matching durable prefix even when a mid-turn
  // commit has not yet persisted optimistic/queued user messages.
  let prefix = 0;
  while (prefix < Math.min(base.length, parsed.length)
      && base[prefix].role === parsed[prefix].role
      && base[prefix].text === parsed[prefix].text) prefix++;
  let next = parsed.map((message, index) => index < prefix
    ? { ...message, id: base[index].id } : message);
  // Confirmation clears the local marker because parsed messages never carry
  // it. Preserve unconfirmed queued users even between turn_end/next turn_start.
  const persistedUsers = parsed.slice(prefix).filter(message => message.role === "user");
  let confirmed = 0;
  const pending = base.slice(prefix).filter(message => {
    if (!message.optimistic || message.role !== "user") return false;
    if (persistedUsers[confirmed]?.text === message.text) { confirmed++; return false; }
    return true;
  });
  next = [...next, ...pending];
  if (new Set(next.map(message => message.id)).size !== next.length) next = parsed;
  // Re-attach the last turn's ephemeral usage (side-slot — the parsed
  // transcript never carries it) to the newest assistant message so the
  // footer outlives the reconcile until the next turn starts.
  if (usage && !live) {
    for (let i = next.length - 1; i >= 0; i--) {
      if (next[i].role === "assistant") {
        next = [...next.slice(0, i), { ...next[i], usage }, ...next.slice(i + 1)];
        break;
      }
    }
  }
  const out = live ? [...next, live] : next;
  let same = out.length === prev.length;
  const shared = out.map((m: Msg, i: number) => {
    const old = prev[i];
    if (old && (old === m || JSON.stringify(old) === JSON.stringify(m))) return old;
    same = false;
    return m;
  });
  return same ? prev : shared;
}

// "starting": the pointer is the host's placeholder — no socket yet.
export type ConnStatus = "starting" | "connecting" | "open" | "closed" | "busy";

export function connStatusOf(st: string): ConnStatus {
  return st === "open" ? "open" : st === "busy" ? "busy"
    : st === "ended" ? "closed" : "connecting";
}


export interface Stream {
  id: string; text: string; thinking: string; tools: ToolActivity[];
  thinkingSections: string[]; inThinking: boolean;
  usage?: Record<string, number>;
}

/** Normalize a wire tool frame (`{id?,name?,status?,input?,result?,kind?,
 *  locations?,content?}` — from a `tool` event or a `history` replay's
 *  `tools[]`) to a ToolActivity. The wire id pairs running↔completion; when
 *  it's absent (a coarse heartbeat) synthesize one from the stream id +
 *  position so the tool-call part still gets a stable toolCallId. */
export function normalizeTool(t: any, streamId: string, k: number): ToolActivity {
  return {
    id: (t && t.id) ? String(t.id) : `${streamId}-t${k}`,
    name: String((t && t.name) || ""),
    status: String((t && t.status) || "running"),
    ...(t && t.input != null && t.input !== "" ? { input: String(t.input) } : {}),
    ...(t && t.result != null && t.result !== "" ? { result: String(t.result) } : {}),
    ...parseToolExtras(t),
  };
}

/** Framework-free reduction; the socket hook owns scheduling and side effects. */
export function reduceFrame(stream: Stream, frame: any): Stream {
  const next: Stream = {...stream, tools: [...stream.tools], thinkingSections: [...stream.thinkingSections]};
  switch (frame.t) {
    case 'history':
      next.text = frame.text || '';
      next.thinking = frame.thinking || '';
      next.thinkingSections = next.thinking ? [next.thinking] : [];
      next.inThinking = !!next.thinking && !frame.text && !(Array.isArray(frame.tools) && frame.tools.length);
      next.tools = Array.isArray(frame.tools) ? frame.tools.map((tool: any, i: number) => normalizeTool(tool, next.id, i)) : [];
      break;
    case 'delta':
      next.inThinking = false;
      next.text += frame.text || '';
      break;
    case 'thinking_delta':
      if (!frame.text) break;
      next.thinking += frame.text;
      if (!next.inThinking || !next.thinkingSections.length) next.thinkingSections.push(frame.text);
      else next.thinkingSections[next.thinkingSections.length - 1] += frame.text;
      next.inThinking = true;
      break;
    case 'tool': {
      next.inThinking = false;
      const at = frame.id ? next.tools.findIndex(tool => tool.id === frame.id) : -1;
      if (at < 0) next.tools.push(normalizeTool(frame, next.id, next.tools.length));
      else {
        const current = next.tools[at];
        const extras = parseToolExtras(frame);
        next.tools[at] = {...current, status: frame.status || current.status,
          ...(frame.name ? {name: String(frame.name)} : {}),
          ...(frame.input != null && frame.input !== '' ? {input: String(frame.input)} : {}),
          ...(frame.result != null && frame.result !== '' ? {result: String(frame.result)} : {}),
          ...(extras.kind ? {kind: extras.kind} : {}),
          ...(extras.locations ? {locations: extras.locations} : {}),
          ...(extras.content ? {content: extras.content} : {})};
      }
      break;
    }
  }
  return next;
}


// ── the queue of messages sent before the chat exists ────────────────
//
// A chat a run has just been accepted for shows the host's starting
// placeholder; what the user sends there cannot ride a socket yet. It queues
// HERE — module-level, keyed by the mounted file (cell + path) — because the
// App remounts its whole runtime tree when the placeholder becomes the real
// pointer (key = chat_id:token), and anything kept in that tree would go with
// it. Each dial of the real session's socket sends what is not yet in
// flight, in order, as user_msg frames when it opens, and marks it as its own;
// the messages stay queued until the dial's attach is confirmed (the host sent
// a frame on it) and go back to waiting when the dial closes first — the host
// accepts a refused attach (4409/4404/4401) and only then closes it, so an
// open alone proves nothing. A queue the run never takes (it ended first) goes
// back into the composer with a banner. In memory only: a reload drops it.

/** `sentBy`: the dial whose socket carried it, awaiting that dial's confirmation. */
export type QueuedMessage = { id: string; text: string; attachments?: Attachment[]; sentBy?: number };

const CHAT_QUEUES = new Map<string, QueuedMessage[]>();
const CHAT_QUEUE_LISTENERS = new Map<string, Set<() => void>>();
let chatQueueSeq = 0;
let chatDialSeq = 0;

export const QUEUE_ABANDONED_TEXT =
  "The run ended before it took your queued messages — they are back in the composer.";

/** The key a file's queue lives under: its cell and its path. */
export function chatQueueKey(api: any): string {
  const cell = typeof api?.getCellId === "function" ? api.getCellId() : "";
  const file = typeof api?.getFilename === "function" ? api.getFilename() : "";
  return `${cell}:${file}`;
}

function notifyChatQueue(key: string) {
  for (const cb of Array.from(CHAT_QUEUE_LISTENERS.get(key) ?? [])) {
    try { cb(); } catch { /* listener gone */ }
  }
}

/** The key's queue, oldest first (an empty array when none). */
export function queuedChat(key: string): QueuedMessage[] {
  return CHAT_QUEUES.get(key) ?? [];
}

/** Queue one message for the key's chat. */
export function enqueueChat(key: string, text: string, attachments?: Attachment[]): QueuedMessage {
  const item: QueuedMessage = { id: `q${++chatQueueSeq}`, text,
    ...(attachments && attachments.length ? { attachments } : {}) };
  CHAT_QUEUES.set(key, [...queuedChat(key), item]);
  notifyChatQueue(key);
  return item;
}

/** Follow the key's queue. Returns unsubscribe. */
export function subscribeChatQueue(key: string, cb: () => void): () => void {
  let set = CHAT_QUEUE_LISTENERS.get(key);
  if (!set) { set = new Set(); CHAT_QUEUE_LISTENERS.set(key, set); }
  set.add(cb);
  return () => {
    set!.delete(cb);
    if (!set!.size) CHAT_QUEUE_LISTENERS.delete(key);
  };
}

/** A queued message as the frame that sends it. */
export function queuedUserFrame(m: QueuedMessage, threadId: string) {
  return { t: "user_msg", content: m.text, thread_id: threadId,
    ...(m.attachments && m.attachments.length ? { attachments: m.attachments } : {}) };
}

/** A fresh owner for one dial's sends (unique across every mounted chat). */
export function newChatDial(): number {
  return ++chatDialSeq;
}

/** Send the key's waiting messages in order through `send`, as `dial`'s;
 *  returns what went out. They stay queued until confirmChatQueue or
 *  requeueChatQueue settles them. A refused send stops the flush and keeps it
 *  and everything after it waiting, so the order holds. While another dial
 *  has messages in flight nothing goes out: that dial settles them first. */
export function flushChatQueue(key: string, send: (frame: any) => boolean, threadId: string,
                               dial: number): QueuedMessage[] {
  const items = queuedChat(key);
  if (items.some((m) => m.sentBy && m.sentBy !== dial)) return [];
  const next: QueuedMessage[] = [];
  const sent: QueuedMessage[] = [];
  let sending = true;
  for (const m of items) {
    if (m.sentBy || !sending) { next.push(m); continue; }
    if (send(queuedUserFrame(m, threadId))) {
      const out = { ...m, sentBy: dial };
      next.push(out);
      sent.push(out);
    } else {
      sending = false;
      next.push(m);
    }
  }
  if (sent.length) {
    CHAT_QUEUES.set(key, next);
    notifyChatQueue(key);
  }
  return sent;
}

/** `dial`'s attach is confirmed: the messages it sent leave the queue
 *  (returned, oldest first, to show as ordinary optimistic bubbles). */
export function confirmChatQueue(key: string, dial: number): QueuedMessage[] {
  const items = queuedChat(key);
  const taken = items.filter((m) => m.sentBy === dial);
  if (!taken.length) return [];
  const rest = items.filter((m) => m.sentBy !== dial);
  if (rest.length) CHAT_QUEUES.set(key, rest);
  else CHAT_QUEUES.delete(key);
  notifyChatQueue(key);
  return taken.map(({ sentBy: _sentBy, ...m }) => m);
}

/** `dial` closed before its attach was confirmed: what it sent waits again,
 *  in place, for the next open socket. */
export function requeueChatQueue(key: string, dial: number): void {
  const items = queuedChat(key);
  if (!items.some((m) => m.sentBy === dial)) return;
  CHAT_QUEUES.set(key, items.map((m) => {
    if (m.sentBy !== dial) return m;
    const { sentBy: _sentBy, ...rest } = m;
    return rest;
  }));
  notifyChatQueue(key);
}

/** Drop the key's queue because the run will never take it: its texts,
 *  joined, to put back in the composer ("" when there was none). */
export function abandonChatQueue(key: string): string {
  const items = queuedChat(key);
  if (!items.length) return "";
  CHAT_QUEUES.delete(key);
  notifyChatQueue(key);
  return items.map((m) => m.text).filter(Boolean).join("\n\n");
}

/** A queued message as the bubble the thread shows under the conversation. */
export function queuedBubble(m: QueuedMessage): Msg {
  return { id: m.id, role: "user", text: m.text, queued: true,
    ...(m.attachments && m.attachments.length ? { attachments: m.attachments } : {}) };
}

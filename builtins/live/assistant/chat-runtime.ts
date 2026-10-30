import React, { useEffect, useMemo, useRef, useState } from "https://esm.sh/react@19.3.1?target=es2022";
import type { AppendMessage, ThreadMessageLike } from "https://esm.sh/@assistant-ui/react@0.15.31?deps=react@19.3.1,react-dom@19.3.1,@assistant-ui/tap@0.9.24,@assistant-ui/core@0.3.31,@assistant-ui/store@0.3.22,radix-ui@1.8.1&target=es2022";
import {useExternalStoreRuntime} from "https://esm.sh/@assistant-ui/react@0.15.31?deps=react@19.3.1,react-dom@19.3.1,@assistant-ui/tap@0.9.24,@assistant-ui/core@0.3.31,@assistant-ui/store@0.3.22,radix-ui@1.8.1&target=es2022";
import {openSessionSocket,sessionWsUrl} from "/api/sdk/session.js";
import {ConnStatus, ThreadEntry, Pointer, ToolLocation, ToolBlock, ToolActivity, Msg, PendingPermission, ConfigOption, normalizePermission, normalizeConfigOptions, ATTACH_DIR, Attachment, UploadStatus, PendingAttachment, guessFromExt, isImageAttachment, sanitizeAttachmentName, dedupeAttachmentName, parseAttachments, parsePointer, parseToolExtras, parseTools, parseTranscript, reconcileTranscript, connStatusOf, Stream, reduceFrame, chatQueueKey, queuedChat, enqueueChat, subscribeChatQueue, newChatDial, flushChatQueue, confirmChatQueue, requeueChatQueue, abandonChatQueue, queuedBubble, QUEUE_ABANDONED_TEXT} from "./message-model";
// Carry the stable id through for message metadata and streaming updates.
// The reasoning part is added ONLY for assistant messages with thinking
// (assistant-ui rejects reasoning parts on other roles), and only when it
// exists — so the "Thinking…" spinner condition (content.length === 0) still
// fires before any delta.
const convertMessage = (m: Msg): ThreadMessageLike => ({
  id: m.id,
  role: m.role,
  content: [
    ...(m.role === "assistant" && m.thinking
      ? [{ type: "reasoning" as const, text: m.thinking }] : []),
    // Tool-call parts (assistant only — assistant-ui rejects them on other
    // roles) route to ToolFallback via assistantParts.tools.Fallback.
    // toolCallId carries our stable id
    // so a completion patch updates the SAME part instead of spawning a new one.
    ...(m.role === "assistant"
      ? (m.tools ?? []).map((t) => ({
          type: "tool-call" as const,
          toolCallId: t.id,
          toolName: t.name || "tool",
          ...(t.input != null ? { argsText: t.input } : {}),
          ...(t.result != null ? { result: t.result } : {}),
        }))
      : []),
    { type: "text" as const, text: m.text },
  ],
});

const textOf = (m: AppendMessage): string =>
  (m.content || [])
    .map((p: any) => (p.type === "text" ? p.text : ""))
    .join("")
    .trim();


// ── native runtime: ExternalStore fed by the chat WebSocket ───────────

// `busy`: the server refused the attach because ANOTHER view holds this
// chat (4409) — the run is alive, this tab just cannot type into it.

// What a non-open chat says, shared by the send-failure banner, the footer
// notice and the composer hint so the three never drift.
export const BUSY_TEXT = "This chat is open in another view — close it there to type here";
export const STOPPED_TEXT = "Generator stopped — this conversation is in the run's log";
export const STARTING_TEXT = "Starting… messages you send now go out when the chat is live";

function createUploadPool(limit: number) {
  let active = 0;
  const waiting: {task: () => Promise<void>; resolve: () => void; reject: (error: unknown) => void}[] = [];
  const pump = () => {
    while (active < limit && waiting.length) {
      const job = waiting.shift()!;
      active++;
      Promise.resolve().then(job.task).then(() => {
        active--; job.resolve(); pump();
      }, error => {
        active--; job.reject(error); pump();
      });
    }
  };
  return (task: () => Promise<void>) => new Promise<void>((resolve, reject) => {
    waiting.push({task, resolve, reject});
    pump();
  });
}

export function useNativeChat(api: any, pointer: Pointer) {
  // Single conversation: the active thread is fixed by the pointer.
  const active = pointer.active_thread;
  const [messages, setMessages] = useState<Msg[]>([]);
  const localIdRef = useRef(0);
  const localId = (prefix: string) => `${prefix}${Date.now()}-${++localIdRef.current}`;
  // The thread with a turn in flight (serve_chat is single-flight), or null.
  const [runningThread, setRunningThread] = useState<string | null>(null);
  const [status, setStatus] = useState<ConnStatus>(pointer.starting ? "starting" : "connecting");
  const [banner, setBanner] = useState<string>("");
  // A benign notice (a queued acknowledgement) renders muted, not as ⚠.
  const [bannerKind, setBannerKind] = useState<"error" | "info">("error");
  // Every non-empty banner names its kind, so an error after a queued notice
  // never inherits the muted style.
  const showBanner = (text: string, kind: "error" | "info" = "error") => {
    setBannerKind(kind);
    setBanner(text);
  };
  // Text to put BACK into the composer: a send the socket could not carry,
  // or a refusal that did not queue the message (the server echoes it).
  const [restoreText, setRestoreText] = useState<{ text: string; n: number } | null>(null);
  // Files uploaded in the composer, waiting to ride the next sent message.
  const [pending, setPending] = useState<PendingAttachment[]>([]);
  const sockRef = useRef<any>(null);  // SessionSocket from /api/sdk/session.js
  const streamsRef = useRef<Record<string, Stream>>({});
  const activeRef = useRef(active);
  activeRef.current = active;
  const messagesRef = useRef<Msg[]>(messages);
  messagesRef.current = messages;
  const uploadPoolRef = useRef<ReturnType<typeof createUploadPool> | null>(null);
  if (!uploadPoolRef.current) uploadPoolRef.current = createUploadPool(3);
  const pendingRef = useRef<PendingAttachment[]>(pending);
  pendingRef.current = pending;
  // Last completed turn's usage per thread — a side-slot (not message state)
  // so the footer survives the turn_end transcript reconcile, which rebuilds
  // messages from disk where usage is deliberately never persisted. Cleared
  // when the next turn starts; lost on reload (ephemeral by design).
  const lastUsageRef = useRef<Record<string, Record<string, number>>>({});
  // ACP session surfaces (agent-v2): the context meter (window occupancy +
  // cumulative session cost — NOT per-turn tokens) and the live plan. Keyed
  // BY THREAD like lastUsageRef: each chat thread owns its own ACP session
  // (agent.py keys the session cache on thread_id), so its window, cost, and
  // plan are per-thread facts — a non-active thread's frames must not paint
  // over the one on screen. Wholesale-replace per
  // event; ephemeral (rebuilt by the next event after a reload).
  const [contextByThread, setContextByThread] = useState<Record<string, {
    used?: number; size?: number; cost?: { amount: number; currency: string };
  }>>({});
  const [planByThread, setPlanByThread] = useState<Record<string,
    { content: string; priority: string; status: string }[]>>({});
  // Pending permission cards (approvals: ask — agent-v2 §2.1): the generator
  // is BLOCKED on each answer, so they also arrive in the reconnect replay's
  // `pending_requests` slot. Answered/expired cards clear on the resolution
  // frame.
  const [permissionRequests, setPermissionRequests] = useState<PendingPermission[]>([]);
  // The session's config options (agent-v2 §2.1's Phase-4 configOptions UI):
  // whatever selects the agent exposes — mode, model, thought_level,
  // arbitrary — rendered generically and set mid-session over the surface
  // WS (`config_set`). Wholesale-replaced by every `config_option` frame
  // (the wire's own semantics) and replayed via the history frame.
  // `configPending` holds the id of the one option awaiting the agent's
  // confirming refresh (its select disables meanwhile).
  const [configOptions, setConfigOptions] = useState<ConfigOption[]>([]);
  const [configPending, setConfigPending] = useState<string>("");
  // The pending change itself ({id, value}) + its bounded timer: a frame
  // that does not yet carry the requested value (the per-turn re-emit of
  // the pre-change set racing the apply) must not snap the select back.
  const configPendingRef = useRef<{ id: string; value: string } | null>(null);
  const configTimerRef = useRef<number | null>(null);
  const clearConfigPending = () => {
    configPendingRef.current = null;
    if (configTimerRef.current) { window.clearTimeout(configTimerRef.current); configTimerRef.current = null; }
    setConfigPending("");
  };
  // Apply an authoritative option set, keeping the optimistic value of a
  // change still awaiting its confirming frame.
  const absorbConfigOptions = (raw: any[]) => {
    const next = normalizeConfigOptions(raw);
    const pend = configPendingRef.current;
    if (!pend) { setConfigOptions(next); return; }
    const confirmed = next.some((o) => o.id === pend.id && o.currentValue === pend.value);
    if (confirmed) { clearConfigPending(); setConfigOptions(next); return; }
    setConfigOptions(next.map((o) => (o.id === pend.id ? { ...o, currentValue: pend.value } : o)));
  };
  // The queue of messages sent while this chat was starting. It
  // lives outside this hook (message-model's module-level store, keyed by
  // the mounted file): the App remounts this whole tree when the starting
  // placeholder becomes the real pointer, and the queue must outlive that.
  const queueKey = chatQueueKey(api);
  const [queued, setQueued] = useState(() => queuedChat(queueKey));
  useEffect(() => {
    setQueued(queuedChat(queueKey));
    return subscribeChatQueue(queueKey, () => setQueued(queuedChat(queueKey)));
  }, [queueKey]);
  // The transcript's path comes from the pointer — its thread entry, else its
  // threads_dir. A pointer naming neither has no history to read.
  const threadFile = React.useCallback((tid: string): string => {
    const entry = pointer.threads.find((t) => t.id === tid);
    if (entry?.file) return entry.file;
    return pointer.threads_dir ? `${pointer.threads_dir}/${tid}.json` : "";
  }, [pointer.threads, pointer.threads_dir]);

  // Load a thread's transcript from its file. While a turn is streaming into
  // THIS thread, the disk transcript doesn't hold the in-flight assistant
  // bubble yet, so a wholesale replace would wipe the accumulated stream —
  // re-materialize it from the thread's stream buffer. reconcileTranscript
  // keeps on-screen ids (no phantom branches) and unchanged message objects.
  //
  // Reads overlap (every files-changed plus turn_end), and fetches can resolve
  // out of order: only the LATEST read applies. A missing transcript (404)
  // parses as empty; any other failure keeps the list on screen — a transient
  // error must not blank the conversation.
  const loadSeqRef = useRef(0);
  useEffect(() => () => { loadSeqRef.current++; }, []);
  const parsedTranscriptsRef = useRef(new Map<string, {raw: string; parsed: Msg[]}>());
  const loadThread = React.useCallback(async (tid: string) => {
    const seq = ++loadSeqRef.current;
    try {
      const file = threadFile(tid);
      const raw = file
        ? await api.getSiblingFile(file).catch((e: any) => {
            if (e?.status === 404) return "";
            throw e;
          })
        : "";
      if (seq !== loadSeqRef.current) return;
      const text = typeof raw === "string" ? raw : "";
      const cached = parsedTranscriptsRef.current.get(tid);
      const parsed = cached?.raw === text ? cached.parsed : parseTranscript(text);
      if (cached?.raw !== text) parsedTranscriptsRef.current.set(tid, {raw: text, parsed});
      setMessages((prev: Msg[]) => {
        const stream = tid === activeRef.current ? streamsRef.current[tid] : undefined;
        const live: Msg | undefined = stream
          ? { id: stream.id, role: "assistant", text: stream.text, thinking: stream.thinking,
              ...(stream.thinkingSections.length > 1
                ? { thinkingSections: [...stream.thinkingSections] } : {}),
              tools: stream.tools, ...(stream.usage ? { usage: stream.usage } : {}) }
          : undefined;
        return reconcileTranscript(prev, parsed, live, lastUsageRef.current[tid]);
      });
    } catch (e) {
      if (seq !== loadSeqRef.current) return;
      console.warn("assistant: transcript read failed; keeping the current list", e);
    }
  }, [api, threadFile]);

  useEffect(() => { loadThread(active); /* initial load */ }, [active, loadThread]);

  // Re-read the active transcript whenever the cell's files change (the
  // generator commits the durable transcript after each turn).
  useEffect(() => {
    let reloadTimer: number | undefined;
    const off = api.onFilesChange?.((detail: { changedFiles: string[] }) => {
      if (!detail.changedFiles.includes(threadFile(activeRef.current))) return;
      window.clearTimeout(reloadTimer);
      reloadTimer = window.setTimeout(() => loadThread(activeRef.current), 150);
    });
    return () => {
      window.clearTimeout(reloadTimer);
      try { off && off(); } catch { /* noop */ }
    };
  }, [api, loadThread, threadFile]);

  // The WebSocket carries live turns + the live thread list. The connection
  // lifecycle (backoff on transient closes, final stop on 4404/4401 with a
  // dead-session marker, slow 4409 takeover poll so two views of one cell
  // don't ping-pong the single-attach session) is openSessionSocket's job —
  // this effect only supplies the frame reducer.
  useEffect(() => {
    if (!pointer.ws_path) { setStatus(pointer.starting ? "starting" : "closed"); return; }
    // Get-or-create the stream buffer for a thread's in-flight turn. Also
    // self-heals after a mid-turn reload, where no turn_start was (re)sent —
    // history/delta frames can still land. The bubble id derives from the
    // TURN id, so a reconnect's history replay reuses the pre-disconnect
    // bubble instead of appending a duplicate.
    const ensureStream = (tid: string, turnId?: string): Stream => {
      let s = streamsRef.current[tid];
      if (!s) {
        s = { id: turnId ? `a-${turnId}` : `a${Date.now()}`, text: "", thinking: "",
              tools: [], thinkingSections: [], inThinking: false };
        streamsRef.current[tid] = s;
      }
      return s;
    };
    const advanceStream = (tid: string, frame: any) => {
      const next = reduceFrame(ensureStream(tid, frame.turn_id), frame);
      streamsRef.current[tid] = next;
      return next;
    };
    // Push the buffer's state into the visible message list — appending the
    // bubble when it isn't there yet (first frame of the turn, or after a
    // transcript reload).
    const paint = (s: Stream) => {
      const extra = {
        // Always set (empty when ≤1 section) so a stream reset in place
        // can CLEAR a previous multi-section split, not only add one.
        thinkingSections: s.thinkingSections.length > 1 ? [...s.thinkingSections] : [],
        ...(s.usage ? { usage: s.usage } : {}),
      };
      setMessages((prev) => prev.some((m) => m.id === s.id)
        ? prev.map((m) => (m.id === s.id
            ? { ...m, text: s.text, thinking: s.thinking, tools: s.tools, ...extra } : m))
        : [...prev, { id: s.id, role: "assistant", text: s.text, thinking: s.thinking, tools: s.tools, ...extra }]);
    };
    let paintFrame: number | null = null;
    let pendingPaint: Stream | null = null;
    const cancelPaint = () => {
      if (paintFrame !== null) cancelAnimationFrame(paintFrame);
      paintFrame = null;
      pendingPaint = null;
    };
    const flushPaint = () => {
      const next = pendingPaint;
      cancelPaint();
      if (next) paint(next);
    };
    const materialize = (s: Stream) => {
      pendingPaint = s;
      if (paintFrame === null) paintFrame = requestAnimationFrame(flushPaint);
    };
    const endStream = (tid: string) => {
      if (pendingPaint?.id === streamsRef.current[tid]?.id) flushPaint();
      delete streamsRef.current[tid];
      setRunningThread((cur) => (cur === tid ? null : cur));
    };
    // The queue sent while the chat was starting rides a dial only once its
    // attach is confirmed. The host accepts a refused attach (4409 another
    // view holds the chat, 4404, 4401) and only then closes it, so `open`
    // proves nothing: each open dial sends what is waiting, as its own, and
    // the first frame the host sends on it confirms the attach — those
    // messages then leave the queue as ordinary optimistic bubbles. A dial
    // that closes first puts them back to wait for the next open socket (this
    // view's redial, or another view's open one, which the queue's change
    // wakes).
    let dial = 0;          // this dial's queue owner while its socket is open
    let dialWs: WebSocket | null = null;
    let confirmed = false;
    const takeQueue = () => {
      const taken = confirmChatQueue(queueKey, dial);
      if (taken.length) {
        setMessages((prev) => [...prev, ...taken.map((m): Msg => ({
          id: localId("u"), role: "user", text: m.text, optimistic: true,
          ...(m.attachments ? { attachments: m.attachments } : {}),
        }))]);
      }
    };
    const flushQueue = () => {
      const ws = dialWs;
      if (!dial || !ws) return;
      flushChatQueue(queueKey, (frame) => {
        try { ws.send(JSON.stringify(frame)); return true; } catch { return false; }
      }, activeRef.current, dial);
      if (confirmed) takeQueue();
    };
    const returnQueue = () => {
      const was = dial;
      dial = 0; dialWs = null; confirmed = false;
      if (was) requeueChatQueue(queueKey, was);
    };
    const offQueue = subscribeChatQueue(queueKey, flushQueue);
    const sock = openSessionSocket({
      url: sessionWsUrl(api, pointer.ws_path, pointer.token),
      connectionKey: pointer.chat_id,
      onStatus: (st: string) => {
        if (st !== "open") returnQueue();
        setStatus(connStatusOf(st));
        if (st !== "open") setRunningThread(null);
        // A dead session never replays: its cards' answers could not be sent.
        if (st === "ended") setPermissionRequests([]);
      },
      onOpen: (ws: WebSocket) => {
        cancelPaint();
        // Reset stale stream state from before a detach: if a turn IS in
        // flight, the host's history replay (or the next delta) rebuilds it.
        // Purge the stale materialized bubbles too — a replay under a NEW
        // turn would otherwise sit next to the pre-disconnect duplicate.
        const stale = new Set(Object.values(streamsRef.current).map((s) => s.id));
        setRunningThread(null);
        // Cards from before the detach may have resolved meanwhile (a
        // permission_resolved emitted while detached is dropped by the host's
        // outbox drain); the history frame replays every still-pending one.
        setPermissionRequests([]);
        streamsRef.current = {};
        if (stale.size) {
          setMessages((prev) => prev.filter((m) => !stale.has(m.id)));
        }
        dial = newChatDial();
        dialWs = ws;
        confirmed = false;
        flushQueue();
      },
      onMessage: (ev: MessageEvent) => {
        // The host's first frame on this dial confirms its attach: what it
        // carried from the queue was taken (before this frame's own effect,
        // so the user bubbles precede the reply they started).
        if (dial && !confirmed) { confirmed = true; takeQueue(); }
        let f: any; try { f = JSON.parse(ev.data); } catch { return; }
        // Turn-scoped frames always carry the turn's bound thread (the SDK
        // is injected fresh into every run, so no frame producer predates
        // thread scoping); frames without one (threads) never use tid.
        const tid: string = f.thread_id || "";
        switch (f.t) {
          case "turn_start": {
            setRunningThread(tid);
            delete lastUsageRef.current[tid];   // the footer describes the PREVIOUS turn
            const s = ensureStream(tid, f.turn_id);
            if (tid === activeRef.current) {
              setBanner("");
              materialize(s);
            }
            break;
          }
          case "history": {
            // Authoritative accumulated partial replayed on (re)connect —
            // sent whenever a turn is in flight, even before its first delta.
            // The replay now also carries the turn's tool activity so far.
            // The frame ALSO arrives turn-less when only retained state
            // (pending permission cards) needs replaying — an empty
            // turn_id must then CLEAR the running marker, not set it to ""
            // (which would read as "busy in another thread" and wedge the
            // composer).
            if (f.turn_id) setRunningThread(tid);
            else setRunningThread(null);
            if (Array.isArray(f.pending_requests)) {
              setPermissionRequests(f.pending_requests
                .map(normalizePermission).filter(Boolean) as PendingPermission[]);
            }
            if (Array.isArray(f.config_options)) {
              // The session's config options survive a reload through the
              // history frame (the switcher must not blank until the next
              // turn's re-emit).
              absorbConfigOptions(f.config_options);
            }
            // A turn-less frame replays retained state only — no stream to fill.
            if (f.turn_id) {
              const s = advanceStream(tid, f);
              if (tid === activeRef.current) materialize(s);
            }
            break;
          }
          case "tool": {
            // A running event carries a bounded input summary; a completion
            // (done/error) carries a bounded result and pairs to its running
            // event by non-empty id. Patch by id — updating status + any newly
            // carried input/result, never blanking a known name (completions
            // may omit it) — else append (a coarse heartbeat with id "" always
            // appends, getting a synthesized position id).
            setRunningThread(tid);
            const s = advanceStream(tid, f);
            if (tid === activeRef.current) materialize(s);
            break;
          }
          case "usage": {
            // One ephemeral per-turn totals frame — attach the numeric fields
            // (strip the t/turn_id/thread_id envelope) onto the in-flight
            // stream so materialize carries them to the bubble; not persisted,
            // so it fades on the next turn_end reconcile.
            const s = streamsRef.current[tid];
            if (!s) break;
            const usage: Record<string, number> = {};
            for (const [k, v] of Object.entries(f)) {
              if (k === "t" || k === "turn_id" || k === "thread_id") continue;
              if (typeof v === "number") usage[k] = v;
            }
            if (Object.keys(usage).length) {
              s.usage = usage;
              lastUsageRef.current[tid] = usage;
              if (tid === activeRef.current) materialize(s);
            }
            break;
          }
          case "delta": {
            if (!f.text) break;
            setRunningThread(tid);
            const s = advanceStream(tid, f);
            if (tid === activeRef.current) materialize(s);
            break;
          }
          case "thinking_delta": {
            if (!f.text) break;
            setRunningThread(tid);
            const s = advanceStream(tid, f);
            if (tid === activeRef.current) materialize(s);
            break;
          }
          case "context": {
            // The ACP context meter — window occupancy + cumulative session
            // cost (NOT per-turn tokens). Per-thread (each thread owns its
            // own ACP session); latest frame for that thread wins.
            const next: any = {};
            if (typeof f.used === "number") next.used = f.used;
            if (typeof f.size === "number") next.size = f.size;
            if (f.cost && typeof f.cost.amount === "number") {
              next.cost = { amount: f.cost.amount, currency: String(f.cost.currency || "") };
            }
            if (Object.keys(next).length) {
              setContextByThread((prev) => ({ ...prev, [tid]: next }));
            }
            break;
          }
          case "config_option": {
            // The session's config options — the wire semantics are
            // wholesale replace (an option the agent withdraws disappears).
            // Every frame is authoritative, so it also clears the pending
            // marker set by an optimistic local change.
            const raw = (f.update && Array.isArray(f.update.configOptions))
              ? f.update.configOptions : null;
            if (raw) absorbConfigOptions(raw);
            break;
          }
          case "current_mode": {
            // A mode change reported by the agent — patch the mode select's
            // current value (the full-set refresh may not follow).
            const modeId = String(f.modeId || "");
            if (modeId) {
              setConfigOptions((prev) => prev.map((o) =>
                o.category === "mode" ? { ...o, currentValue: modeId } : o));
              const pend = configPendingRef.current;
              if (!pend || (pend.value === modeId)) clearConfigPending();
            }
            break;
          }
          case "plan": {
            // The agent's live plan (ACP `plan` updates) — wholesale replace,
            // scoped to the emitting turn's bound thread.
            const entries = Array.isArray(f.entries) ? f.entries : [];
            const cleaned = entries
              .filter((e: any) => e && typeof e === "object" && e.content)
              .map((e: any) => ({
                content: String(e.content), priority: String(e.priority || ""),
                status: String(e.status || ""),
              }));
            setPlanByThread((prev) => ({ ...prev, [tid]: cleaned }));
            break;
          }
          case "permission_request": {
            const req = normalizePermission(f);
            if (!req) break;
            setPermissionRequests((prev) =>
              [...prev.filter((r) => r.id !== req.id), req]);
            break;
          }
          case "permission_resolved": {
            if (!f.id) break;
            setPermissionRequests((prev) => prev.filter((r) => r.id !== f.id));
            break;
          }
          case "turn_end":
            endStream(tid);
            if (tid === activeRef.current) {
              loadThread(tid);   // reconcile from the durable transcript
            }
            break;
          case "refused":
            if (tid === activeRef.current) showBanner(String(f.message || "The request was refused."));
            break;
          case "busy": {
            // Server refusal (single-flight) or a QUEUED
            // acknowledgement (the message will run after the current
            // turn). A queued message is fine — a muted notice. A refused
            // one goes back into the composer (the server echoes it), and
            // its optimistic bubble comes down: nothing was recorded.
            if (tid === activeRef.current) {
              if (f.queued) {
                showBanner(String(f.message || "Your message is queued."), "info");
              } else {
                showBanner(String(f.message || "The assistant is still replying."));
                if (f.content) {
                  const echoed = String(f.content);
                  setRestoreText((r) => ({ text: echoed, n: (r?.n ?? 0) + 1 }));
                  setMessages((prev) => {
                    const last = prev[prev.length - 1];
                    return last && last.role === "user" && last.id.startsWith("u")
                      && last.text === echoed ? prev.slice(0, -1) : prev;
                  });
                }
              }
            }
            break;
          }
          case "error":
            // Surface as a dismissable banner instead of a fake transcript message.
            endStream(tid);
            if (tid === activeRef.current) {
              showBanner(f.message || "error");
              loadThread(tid);
            }
            break;
        }
      },
    });
    sockRef.current = sock;
    // Cleanup CLOSES the socket (final — no redial), freeing the host's
    // single-attach session for whichever view mounts next.
    return () => { cancelPaint(); offQueue(); returnQueue(); sockRef.current = null; sock.close(); };
  }, [pointer.ws_path, pointer.token, pointer.chat_id, pointer.starting, loadThread, queueKey]);

  const send = (obj: any): boolean => sockRef.current?.sendJson(obj) ?? false;

  // A queue the run never takes: the session ended (a live pointer) with
  // messages still waiting — an ended dial put back what it had not had
  // confirmed — or, still on the placeholder, the host released the run's
  // claim on this file without the run registering it. The text goes back
  // into the composer, with a banner.
  const abandonQueue = React.useCallback(() => {
    const text = abandonChatQueue(queueKey);
    if (!text) return;
    showBanner(QUEUE_ABANDONED_TEXT);
    setRestoreText((r) => ({ text, n: (r?.n ?? 0) + 1 }));
  }, [queueKey]);
  useEffect(() => {
    if (status === "closed" && !pointer.starting) abandonQueue();
  }, [status, pointer.starting, abandonQueue]);
  useEffect(() => {
    if (!pointer.starting || typeof api.onSurfaceChange !== "function"
        || typeof api.getSurface !== "function") return;
    let current = true;
    const off = api.onSurfaceChange(() => {
      Promise.resolve(api.getSurface()).then((surface: unknown) => {
        if (current && !surface) abandonQueue();
      }, () => { /* a failed lookup is not a release */ });
    });
    return () => { current = false; try { off && off(); } catch { /* noop */ } };
  }, [api, pointer.starting, abandonQueue]);

  // Sends queue while the chat is starting (the pointer is the host's
  // placeholder); the real session's first confirmed attach takes them.
  const queueing = pointer.starting;

  const runningVisible = runningThread !== null && runningThread === active;

  // Send a user turn. Reached two ways: assistant-ui's composer (Enter / its
  // built-in Send, via onNew) for text, and our attach-aware Send button, which
  // also fires when there's an attachment but no text. Only finished uploads
  // (status "done") ride along — the Send button is disabled while an upload is
  // in flight, so skipping the others here is just a backstop. Clears the
  // pending tray on a successful send.
  const submit = (rawText: string) => {
    const text = (rawText || "").trim();
    const ready = pendingRef.current.filter((a) => a.status === "done");
    if (!text && !ready.length) return;   // allow text OR at least one attachment
    const atts: Attachment[] = ready.map((a) => ({ path: a.path, name: a.name, media_type: a.media_type }));
    if (queueing) {
      // No socket yet: queue it (shown as a queued bubble) for the first
      // confirmed attach.
      enqueueChat(queueKey, text, atts);
      setBanner("");
      setPending((prev) => prev.filter((a) => a.status === "uploading"));
      return;
    }
    // Always a user_msg: while a reply streams the server QUEUES it behind
    // the current turn (agent-v2 §9.1.3) and acknowledges with a queued
    // notice; Stop is the way to cut a reply short.
    if (!send({ t: "user_msg", content: text,
                thread_id: activeRef.current,
                ...(atts.length ? { attachments: atts } : {}) })) {
      // The socket could not carry it: keep the text in the composer and
      // say what the state actually is.
      showBanner(status === "connecting"
        ? "Still connecting — try again in a moment."
        : status === "busy"
          ? BUSY_TEXT + "."
          : STOPPED_TEXT + ".");
      if (text) setRestoreText((r) => ({ text, n: (r?.n ?? 0) + 1 }));
      return;
    }
    setBanner("");
    setMessages((prev) => [...prev, {
      id: localId("u"), role: "user", text, optimistic: true,
      ...(atts.length ? { attachments: atts } : {}),
    }]);
    // Drop what we sent (done) and any failed chips; KEEP still-uploading ones
    // (the Send button is disabled mid-upload, but the Enter key isn't — so this
    // is how an in-flight upload survives an Enter-send to ride the next one).
    setPending((prev) => prev.filter((a) => a.status === "uploading"));
  };

  const onNew = async (message: AppendMessage) => { submit(textOf(message)); };

  // Upload each picked file into the cell (attachments/<name>) and stage it as
  // a pending chip. De-dupes names within the batch AND against already-pending
  // ones so two "photo.png" picks don't clobber. Never throws: a failed upload
  // becomes a red chip + banner, and the file is skipped at send time.
  const attachFiles = async (files: FileList | File[] | null | undefined) => {
    const list: File[] = files ? Array.from(files as any) : [];
    if (!list.length) return;
    // Unique across the WHOLE conversation, not just the pending tray: a
    // second message's photo.png must not overwrite the first message's.
    const taken = new Set([
      ...pendingRef.current.map((a) => a.path.slice(a.path.lastIndexOf("/") + 1)),
      ...messagesRef.current.flatMap((m) => (m.attachments ?? []).map((a) => a.path.slice(a.path.lastIndexOf("/") + 1))),
    ]);
    const jobs = list.map(file => {
      const onDisk = dedupeAttachmentName(sanitizeAttachmentName(file.name), taken);
      taken.add(onDisk);
      const entry: PendingAttachment = {
        path: `${ATTACH_DIR}/${onDisk}`, name: file.name || onDisk,
        media_type: file.type || guessFromExt(file.name || onDisk), status: "uploading",
      };
      return {file, entry};
    });
    pendingRef.current = [...pendingRef.current, ...jobs.map(job => job.entry)];
    setPending(previous => [...previous, ...jobs.map(job => job.entry)]);
    const upload = async ({file, entry}: typeof jobs[number]) => {
        try {
          const buffer = await file.arrayBuffer();
          const ok = api.stageDraftFile ? await api.stageDraftFile(entry.path, buffer) : false;
          setPending(previous => previous.map(attachment => attachment.path === entry.path
            ? {...attachment, status: ok ? "done" : "error"} : attachment));
          if (!ok) showBanner(`Couldn't upload “${file.name}”.`);
        } catch (error: any) {
          setPending(previous => previous.map(attachment => attachment.path === entry.path
            ? {...attachment, status: "error"} : attachment));
          showBanner(`Couldn't upload “${file.name}”: ${error?.message || error}`);
        }
    };
    await Promise.all(jobs.map(job => uploadPoolRef.current!(() => upload(job))));
  };
  // Drop a staged attachment. Harmless to leave the uploaded draft in the cell.
  const removePending = (path: string) => setPending((prev) => prev.filter((a) => a.path !== path));

  // The thread shows queued prompts as bubbles under the conversation.
  const shown = useMemo(() => (queued.length ? [...messages, ...queued.map(queuedBubble)] : messages),
    [messages, queued]);

  const runtime = useExternalStoreRuntime({
    messages: shown,
    // Running means THIS thread's turn — a background turn in another thread
    // doesn't flip the visible composer to its stop state.
    isRunning: runningVisible,
    convertMessage,
    onNew,
    // `runningThread` stays set until the generator's turn_end actually arrives.
    // thread_id scopes the cancel to the visible thread's turn.
    onCancel: async () => { send({ t: "interrupt", thread_id: activeRef.current }); },
    // The queued bubbles are the queue's, never message state.
    setMessages: (ms: readonly Msg[]) => setMessages((ms as Msg[]).filter((m) => !m.queued)),
    unstable_capabilities: { copy: true },
  });

  // Stable identities (useCallback) so the memoized footer children skip
  // stream frames; the bodies read only refs, stable setters, and `send` /
  // `clearConfigPending`, which themselves read only refs and setters.
  const setConfigOption = React.useCallback((id: string, value: string) => {
    const sent = send({ t: "config_set", config_id: id, value });
    if (sent) {
      clearConfigPending();
      configPendingRef.current = { id, value };
      setConfigPending(id);
      setConfigOptions((prev) => prev.map((o) =>
        o.id === id ? { ...o, currentValue: value } : o));
      // Bounded pending: if no confirming frame ever arrives (the apply
      // failed agent-side, say), re-enable the select rather than wedge it
      // until the next turn's re-emit. One timer per change — a second
      // change re-arms it.
      configTimerRef.current = window.setTimeout(() => {
        if (configPendingRef.current?.id === id) clearConfigPending();
      }, 12000);
    }
    return sent;
  }, []);
  const answerPermission = React.useCallback((id: string, chosen: string | null) => {
    const sent = send({ t: "permission_answer", id,
      ...(chosen ? { option_id: chosen } : { cancelled: true }) });
    if (sent) {
      setPermissionRequests((prev) => prev.filter((r) => r.id !== id));
    }
    return sent;
  }, []);

  return {
    runtime, messages: shown, status, banner, bannerKind, restoreText,
    running: runningVisible,
    // Sends queue: the chat is starting.
    queueing,
    dismissBanner: () => setBanner(""),
    attachments: { pending, attach: attachFiles, remove: removePending, submit },
    // The VIEWED thread's surfaces only — a non-active thread's plan/meter
    // stay in their slot and never render.
    context: contextByThread[active] ?? null,
    plan: planByThread[active] ?? [],
    // The generic configOptions UI (agent-v2 §2.1 Phase 4): setConfigOption
    // sends the change over the surface WS and marks the option pending
    // (optimistic current value); the agent's next config_option /
    // current_mode frame is the authoritative refresh.
    configOptions,
    configPending,
    setConfigOption,
    permissionRequests,
    // Answer a permission card over the surface WS request/response frame
    // (never the action queue — agent-v2 §4.3.4). Optimistically clears the
    // card; the generator's resolution frame is the authoritative clear.
    answerPermission,
  };
}

/**
 * Idle scheduler for the guest pool's read→write sync pass.
 *
 * The pool positions its elements by copying each container's bounding
 * rect onto the element it hosts, once per frame, after Motion has
 * applied that frame's transforms. That pass used to reschedule itself
 * unconditionally — a permanent `frame.postRender` callback. Motion's
 * batcher treats a standing callback as a reason to keep the whole
 * frameloop alive (`allowKeepAlive`), so a motionless canvas with an
 * empty pool still ran all eight scheduler steps on every rAF, forever.
 * The cost is not the pass; it is the frameloop the pass pins awake.
 *
 * So the loop is now earned rather than assumed, as a three-state
 * machine:
 *
 *   idle      nothing scheduled. Zero rAF callbacks. The canvas is
 *             quiet and the browser is allowed to notice.
 *   active    a frame callback is queued and the last pass moved
 *             something.
 *   settling  a frame callback is still queued, but the last N passes
 *             found nothing to write. Runs out the clock, then parks.
 *
 * The asymmetry is deliberate. **Waking is cheap and over-eager**: any
 * caller that touched an entry, any camera motion, any React commit may
 * call `invalidate()`, and the worst case is a handful of passes that
 * find nothing. **Stopping is earned**: only `settleFrames` consecutive
 * clean passes park the loop. A missed wake is a guest visibly lagging
 * its cell; a spurious wake is a few microseconds.
 *
 * The quiescence signal costs nothing to produce, because the pool
 * already has it. Its per-entry lastX/Y/W/H cache exists to skip style
 * writes on frames where a rect hasn't moved; `pass()` simply reports
 * whether that cache was beaten. Nothing here has to know that a spring
 * is running, how long it will take, or when it is "done" — a spring
 * keeps re-writing rects for its whole settle, every one of those frames
 * reads as dirty, and the loop stays alive until the spring genuinely
 * stops moving pixels. No duration is tracked or guessed anywhere.
 *
 * The verify tick is the safety net for the one failure this design can
 * have: a mutation nobody thought to wire a wake source to. (The pool's
 * pass also counts an element that no longer holds what the pool last
 * wrote as work, so the same tick puts back an element displaced by any
 * writer at all — an offset renderer lasts at most one period.) It runs the
 * same pass on a plain `setInterval` — NEVER a rAF, which would wake the
 * batcher and reintroduce exactly the always-on frameloop this module
 * exists to remove — and if it finds work while the loop is parked, that
 * is a bug: it fixes the position (so the user never sees the lag), then
 * counts it and reports it through `onMissedWake`. The counter is the
 * point. A wake source we forgot shows up as `missedWakes > 0` in
 * `stats()` rather than as an intermittent "the renderer is in the wrong
 * place" report six months later.
 *
 * Every effect arrives through the deps bag, with no module-level
 * imports of `frame`, `window` or a clock, so the whole machine —
 * including the park and missed-wake paths, which are miserable to
 * reproduce for real — can be driven deterministically by a test.
 */

/** Where the loop is right now. See the module doc. */
export type GuestSyncState = 'idle' | 'active' | 'settling';

export interface GuestSyncStats {
  /** Every pass the scheduler ran — frame passes and verify passes. */
  passes: number;
  /** Parked → running transitions. One per genuine idle-to-motion edge. */
  wakes: number;
  /** Verify ticks that found work while parked. Should stay 0; anything
   *  else names a mutation with no wake source. */
  missedWakes: number;
}

export interface GuestSyncDeps {
  /** One batched read→write pass. Returns true if any entry was
   *  re-written — i.e. the scene is still moving. */
  pass: () => boolean;
  /** Motion's `frame.postRender`. Must run the callback after the
   *  frame's transforms are applied, and must wake a parked batcher. */
  scheduleFrame: (cb: () => void) => void;
  /** Motion's `cancelFrame`. */
  cancelFrame: (cb: () => void) => void;
  /** Injected clock — `window.setInterval` in the app, a manual queue in
   *  tests. Deliberately not a rAF: see the module doc. */
  setInterval: (cb: () => void, ms: number) => number;
  clearInterval: (handle: number) => void;
  /** Consecutive clean passes before the loop parks. Default 12
   *  (~200ms at 60fps) — long enough to ride out a spring's last few
   *  sub-pixel frames, short enough that idle means idle. */
  settleFrames?: number;
  /** Verify-tick period. Default 500ms. */
  verifyMs?: number;
  /** A verify tick found work while parked: a wake source is missing. */
  onMissedWake?: () => void;
  /**
   * The pass has just been clean for `settleFrames` consecutive frames —
   * the scene is genuinely at rest. Fired once per quiescence edge (also
   * under `alwaysRun`, where the loop keeps running but the scene is no
   * less still). The guest pool uses this as its "camera at rest" signal
   * to bring frozen native guest views back to live.
   */
  onSettle?: () => void;
  /** Rollback switch — never park, exactly like the old always-on loop.
   *  With this set the verify tick stands down entirely (the loop is
   *  always running, so there is nothing for it to catch). */
  alwaysRun?: boolean;
}

export interface GuestSyncScheduler {
  /** Something moved, or might have. Idempotent, cheap, and safe to
   *  call from any context — including from inside `pass()` itself. */
  invalidate: () => void;
  state: () => GuestSyncState;
  stats: () => GuestSyncStats;
  /** True once the pass has been clean for `settleFrames` frames and no
   *  invalidation has arrived since — the same edge `onSettle` announces.
   *  This is the pool's "camera at rest" predicate. */
  atRest: () => boolean;
  /** Cancels the queued frame and the verify tick. After this,
   *  `invalidate()` schedules nothing. */
  stop: () => void;
}

/** ~200ms at 60fps. */
const DEFAULT_SETTLE_FRAMES = 12;
const DEFAULT_VERIFY_MS = 500;

export function createGuestSyncScheduler(deps: GuestSyncDeps): GuestSyncScheduler {
  const settleFrames = deps.settleFrames ?? DEFAULT_SETTLE_FRAMES;
  const verifyMs = deps.verifyMs ?? DEFAULT_VERIFY_MS;
  const alwaysRun = deps.alwaysRun === true;

  /** A frame callback is queued right now. This — not a separate flag —
   *  is what "running" means; parking is precisely the absence of a
   *  queued callback. */
  let frameQueued = false;
  let stopped = false;
  /** Consecutive clean passes. Reset by any dirty pass and by
   *  `invalidate()`. */
  let quiet = 0;
  let passes = 0;
  let wakes = 0;
  let missedWakes = 0;

  // Mutually recursive, so both are declarations rather than consts.
  function schedule(): void {
    // Guarded rather than assumed: `pass()` may call `invalidate()`
    // re-entrantly (a ResizeObserver firing inside a layout read, say),
    // and two queued callbacks for one frame would double the work
    // every frame for as long as the loop ran.
    if (frameQueued) return;
    frameQueued = true;
    deps.scheduleFrame(tick);
  }

  function tick(): void {
    if (stopped) return;
    // The queued callback has fired; anything scheduled from here on is
    // a *new* frame.
    frameQueued = false;
    passes++;
    if (deps.pass()) quiet = 0;
    else quiet++;
    // Exactly the quiescence edge, announced whether or not the loop is
    // about to park (under alwaysRun it never parks, but the scene is no
    // less at rest for it).
    if (quiet === settleFrames) deps.onSettle?.();
    if (alwaysRun || quiet < settleFrames) schedule();
    // Otherwise: park. Nothing queued, nothing pinning the frameloop.
  }

  const invalidate = () => {
    if (stopped) return;
    // Reset the settle clock even when the loop is already running —
    // that is what makes a wake during `settling` re-arm the full tail
    // rather than letting the loop park mid-motion.
    quiet = 0;
    if (frameQueued) return;
    wakes++;
    // Motion's schedule() wakes a parked batcher, so this restarts the
    // frameloop correctly from any context — an event handler, a React
    // commit, a motion value subscription, or the verify tick.
    schedule();
  };

  const verify = () => {
    if (stopped) return;
    // The loop is already running: it will see anything this tick could.
    if (frameQueued) return;
    // Never parks, so a missed wake is not a thing that can happen —
    // and running the pass here would only duplicate work.
    if (alwaysRun) return;
    passes++;
    if (!deps.pass()) return;
    // Parked, yet a rect had moved. The position is now fixed (the pass
    // wrote it), but the wake that should have caught it never came.
    missedWakes++;
    deps.onMissedWake?.();
    invalidate();
  };

  let verifyHandle: number | null = deps.setInterval(verify, verifyMs);

  return {
    invalidate,
    state: () => (frameQueued ? (quiet === 0 ? 'active' : 'settling') : 'idle'),
    stats: () => ({ passes, wakes, missedWakes }),
    atRest: () => quiet >= settleFrames,
    stop: () => {
      if (stopped) return;
      stopped = true;
      if (frameQueued) {
        deps.cancelFrame(tick);
        frameQueued = false;
      }
      if (verifyHandle !== null) {
        deps.clearInterval(verifyHandle);
        verifyHandle = null;
      }
    },
  };
}

/** localStorage key for the rollback switch. */
const ALWAYS_RUN_KEY = 'collimate.guestSync';

/**
 * Rollback switch: `?guestsync=always` in the URL, or
 * `localStorage['collimate.guestSync'] === 'always'`.
 *
 * Present in PRODUCTION builds on purpose. The failure this guards
 * against is a guest visibly lagging the cell it sits in — a missing
 * wake source for some interaction we didn't think of — and that is by
 * construction a thing nobody can reproduce in dev, because in dev it
 * doesn't happen. A user hitting it needs a way to get the old always-on
 * loop back inside one page reload, without a release; and the URL form
 * means the instruction fits in a single link.
 */
export function guestSyncAlwaysRun(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    if (new URLSearchParams(window.location.search).get('guestsync') === 'always') return true;
  } catch {
    /* exotic location — fall through to storage */
  }
  try {
    return window.localStorage?.getItem(ALWAYS_RUN_KEY) === 'always';
  } catch {
    /* storage blocked (private mode, third-party cookie policy) */
  }
  return false;
}

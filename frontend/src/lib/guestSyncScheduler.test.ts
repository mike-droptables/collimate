/**
 * The guest-sync scheduler's whole job is an absence: a quiet canvas
 * must produce ZERO scheduled frames. Everything below is driven through
 * the deps bag with a manual frame queue and a manual clock — no fake
 * timers, no rAF, no real time — so "parked" is directly observable as
 * an empty queue rather than inferred from a timeout that didn't fire.
 */
import { describe, it, expect, vi } from 'vitest';
import {
  createGuestSyncScheduler,
  guestSyncAlwaysRun,
  type GuestSyncDeps,
  type GuestSyncScheduler,
} from './guestSyncScheduler';

/** Manual frame queue + manual clock, standing in for motion's frameloop
 *  and window.setInterval. */
function harness(overrides: Partial<GuestSyncDeps> = {}) {
  const queue: Array<() => void> = [];
  const intervals = new Map<number, { cb: () => void; ms: number }>();
  let nextHandle = 1;
  /** Flipped by a test to make the next pass report movement. */
  let dirty = false;

  const pass = vi.fn(() => dirty);

  const deps: GuestSyncDeps = {
    pass,
    scheduleFrame: (cb) => { queue.push(cb); },
    cancelFrame: (cb) => {
      const i = queue.indexOf(cb);
      if (i !== -1) queue.splice(i, 1);
    },
    setInterval: (cb, ms) => {
      const handle = nextHandle++;
      intervals.set(handle, { cb, ms });
      return handle;
    },
    clearInterval: (handle) => { intervals.delete(handle); },
    ...overrides,
  };

  const sched = createGuestSyncScheduler(deps);

  return {
    sched,
    pass,
    /** Frame callbacks waiting to run. Zero means parked. */
    queued: () => queue.length,
    setDirty: (v: boolean) => { dirty = v; },
    /** Run every currently-queued frame callback once, the way a single
     *  rAF flush would. Callbacks scheduled during the flush wait for
     *  the next one. */
    flushFrame: () => {
      const due = queue.splice(0, queue.length);
      for (const cb of due) cb();
    },
    /** Run `n` frames back to back, stopping early once parked. */
    flushFrames: (n: number) => {
      for (let i = 0; i < n; i++) {
        if (queue.length === 0) break;
        const due = queue.splice(0, queue.length);
        for (const cb of due) cb();
      }
    },
    /** Fire every registered interval once. */
    tickVerify: () => {
      for (const { cb } of Array.from(intervals.values())) cb();
    },
    intervalCount: () => intervals.size,
    verifyMs: () => Array.from(intervals.values())[0]?.ms,
  };
}

/** Drive to a park from a standing start, asserting nothing is queued. */
function parkIt(h: ReturnType<typeof harness>, settleFrames = 12): void {
  h.sched.invalidate();
  h.setDirty(false);
  h.flushFrames(settleFrames + 2);
  expect(h.queued()).toBe(0);
}

describe('createGuestSyncScheduler', () => {
  it('starts idle with nothing scheduled', () => {
    const h = harness();
    expect(h.sched.state()).toBe('idle');
    expect(h.queued()).toBe(0);
    expect(h.pass).not.toHaveBeenCalled();
    expect(h.sched.stats()).toEqual({ passes: 0, wakes: 0, missedWakes: 0 });
    // The verify tick is the one thing that runs unbidden — and it is a
    // plain interval, which pins nothing awake.
    expect(h.intervalCount()).toBe(1);
    expect(h.verifyMs()).toBe(500);
  });

  it('goes active on invalidate, and queues exactly one frame however often it is called', () => {
    const h = harness();
    h.setDirty(true);

    h.sched.invalidate();
    expect(h.sched.state()).toBe('active');
    expect(h.queued()).toBe(1);

    h.sched.invalidate();
    h.sched.invalidate();
    expect(h.queued()).toBe(1);
    // Only the parked→running edge counts as a wake.
    expect(h.sched.stats().wakes).toBe(1);
  });

  it('never parks while passes keep coming back dirty', () => {
    const h = harness();
    h.setDirty(true);
    h.sched.invalidate();

    for (let i = 0; i < 200; i++) {
      h.flushFrame();
      expect(h.queued()).toBe(1);
      expect(h.sched.state()).toBe('active');
    }
    expect(h.sched.stats().passes).toBe(200);
  });

  it('settles and parks on exactly the settleFrames-th clean pass', () => {
    const settleFrames = 12;
    const h = harness({ settleFrames });
    h.setDirty(true);
    h.sched.invalidate();
    h.flushFrame();                       // one dirty pass
    expect(h.sched.state()).toBe('active');

    h.setDirty(false);
    for (let i = 1; i < settleFrames; i++) {
      h.flushFrame();
      // Clean, but the tail hasn't run out — still queued, now settling.
      expect(h.sched.state()).toBe('settling');
      expect(h.queued()).toBe(1);
    }

    h.flushFrame();                       // the settleFrames-th clean pass
    expect(h.sched.state()).toBe('idle');
    expect(h.queued()).toBe(0);
    expect(h.sched.stats().passes).toBe(settleFrames + 1);

    // And it stays parked: nothing re-queues itself behind our back.
    h.flushFrames(5);
    expect(h.queued()).toBe(0);
    expect(h.sched.stats().passes).toBe(settleFrames + 1);
  });

  it('re-arms the whole tail when invalidated mid-settle', () => {
    const settleFrames = 12;
    const h = harness({ settleFrames });
    h.sched.invalidate();
    h.setDirty(false);
    h.flushFrames(settleFrames - 1);
    expect(h.sched.state()).toBe('settling');

    h.sched.invalidate();
    expect(h.sched.state()).toBe('active');
    // One frame short of the original park, so if the counter had not
    // reset this would park on the very next pass.
    h.flushFrames(settleFrames - 1);
    expect(h.queued()).toBe(1);
    expect(h.sched.state()).toBe('settling');

    h.flushFrame();
    expect(h.sched.state()).toBe('idle');
  });

  it('leaves a parked-and-clean loop alone on a verify tick', () => {
    const h = harness();
    parkIt(h);
    const before = h.sched.stats().passes;

    h.tickVerify();

    expect(h.sched.state()).toBe('idle');
    // The verify pass ran (that is how it knows), but found nothing and
    // scheduled no frame — the whole point.
    expect(h.sched.stats().passes).toBe(before + 1);
    expect(h.queued()).toBe(0);
    expect(h.sched.stats().missedWakes).toBe(0);
  });

  it('catches a mutation with no wake source, and reports it', () => {
    const onMissedWake = vi.fn();
    const h = harness({ onMissedWake });
    parkIt(h);

    // Something moved an entry without invalidating — the failure mode
    // the verify tick exists for.
    h.setDirty(true);
    h.tickVerify();

    expect(onMissedWake).toHaveBeenCalledTimes(1);
    expect(h.sched.stats().missedWakes).toBe(1);
    expect(h.sched.state()).toBe('active');
    expect(h.queued()).toBe(1);

    // And the loop is genuinely back: frames resume from here.
    h.flushFrame();
    expect(h.queued()).toBe(1);
  });

  it('stands down on a verify tick while a frame is already queued', () => {
    const onMissedWake = vi.fn();
    const h = harness({ onMissedWake });
    h.setDirty(true);
    h.sched.invalidate();
    const before = h.sched.stats().passes;

    h.tickVerify();

    expect(h.sched.stats().passes).toBe(before);
    expect(h.pass).not.toHaveBeenCalled();
    expect(onMissedWake).not.toHaveBeenCalled();
    expect(h.queued()).toBe(1);
  });

  it('never parks under alwaysRun, and never runs a verify pass there', () => {
    const onMissedWake = vi.fn();
    const h = harness({ alwaysRun: true, settleFrames: 3, onMissedWake });
    h.sched.invalidate();
    h.setDirty(false);

    h.flushFrames(20);
    expect(h.queued()).toBe(1);
    expect(h.sched.stats().passes).toBe(20);

    const before = h.sched.stats().passes;
    h.tickVerify();
    // Nothing for it to catch — the loop is always running.
    expect(h.sched.stats().passes).toBe(before);
    expect(h.sched.stats().missedWakes).toBe(0);
    expect(onMissedWake).not.toHaveBeenCalled();
  });

  it('cancels everything on stop, and stays stopped', () => {
    const h = harness();
    h.setDirty(true);
    h.sched.invalidate();
    expect(h.queued()).toBe(1);

    h.sched.stop();

    expect(h.queued()).toBe(0);
    expect(h.intervalCount()).toBe(0);
    expect(h.sched.state()).toBe('idle');

    const passes = h.sched.stats().passes;
    h.sched.invalidate();
    expect(h.queued()).toBe(0);
    expect(h.sched.stats().wakes).toBe(1);   // the pre-stop one, and no more
    expect(h.sched.stats().passes).toBe(passes);

    // Idempotent — an unmount racing a stop must not throw.
    expect(() => h.sched.stop()).not.toThrow();
  });

  it('does not double-schedule when the pass invalidates re-entrantly', () => {
    // A ResizeObserver firing inside the pass's layout reads is the real
    // shape of this; two queued callbacks would double every frame's work.
    const self: { sched: GuestSyncScheduler | null } = { sched: null };
    const h = harness({
      pass: vi.fn(() => {
        self.sched?.invalidate();
        return false;
      }),
    });
    self.sched = h.sched;

    h.sched.invalidate();
    for (let i = 0; i < 20; i++) {
      h.flushFrame();
      // One frame in, one frame out — never two.
      expect(h.queued()).toBe(1);
    }
    // Exactly one pass per frame, plus the invalidate that started it.
    expect(h.sched.stats().passes).toBe(20);
    // And the re-entrant invalidate keeps resetting the settle clock, so
    // the loop can't park while the pass keeps calling it.
    expect(h.sched.state()).toBe('settling');
  });

  it('honours a custom verify period', () => {
    const h = harness({ verifyMs: 50 });
    expect(h.verifyMs()).toBe(50);
  });
});

describe('guestSyncAlwaysRun', () => {
  // Node project — no window at all, which is also the SSR case.
  it('is false when there is no window', () => {
    expect(guestSyncAlwaysRun()).toBe(false);
  });

  it('reads the query param and the localStorage key', () => {
    const store = new Map<string, string>();
    const fakeWindow = {
      location: { search: '?guestsync=always' },
      localStorage: { getItem: (k: string) => store.get(k) ?? null },
    };
    vi.stubGlobal('window', fakeWindow);
    try {
      expect(guestSyncAlwaysRun()).toBe(true);

      fakeWindow.location.search = '?other=1';
      expect(guestSyncAlwaysRun()).toBe(false);

      store.set('collimate.guestSync', 'always');
      expect(guestSyncAlwaysRun()).toBe(true);

      store.set('collimate.guestSync', 'no');
      expect(guestSyncAlwaysRun()).toBe(false);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('survives storage being blocked', () => {
    vi.stubGlobal('window', {
      location: { search: '' },
      get localStorage(): Storage { throw new Error('blocked'); },
    });
    try {
      expect(guestSyncAlwaysRun()).toBe(false);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('falls through to storage when the location is unreadable', () => {
    // A rollback switch that throws instead of answering would be worse
    // than one that says no.
    vi.stubGlobal('window', {
      get location(): Location { throw new Error('cross-origin'); },
      localStorage: { getItem: () => 'always' },
    });
    try {
      expect(guestSyncAlwaysRun()).toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

// @vitest-environment jsdom
import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest';
import {
  installRendererInputContainment,
  registerRendererModule,
  registerRendererInstance,
  _setAttributorForTests,
} from './rendererInputContainment';

// The patch is prototype-global and idempotent; install once for the file.
beforeAll(() => {
  installRendererInputContainment();
});

afterEach(() => {
  _setAttributorForTests(null);
});

const MOD = 'blob:http://test/renderer-abc';

function asRenderer<T>(fn: () => T): T {
  _setAttributorForTests(() => MOD);
  try { return fn(); } finally { _setAttributorForTests(() => null); }
}

describe('rendererInputContainment', () => {
  it('gates a renderer-registered input listener on instance focus', () => {
    registerRendererModule(MOD);
    let focused = false;
    const unreg = registerRendererInstance(MOD, () => focused);
    const seen = vi.fn();
    asRenderer(() => window.addEventListener('keydown', seen));

    window.dispatchEvent(new KeyboardEvent('keydown', { key: ' ' }));
    expect(seen).not.toHaveBeenCalled();

    focused = true;
    window.dispatchEvent(new KeyboardEvent('keydown', { key: ' ' }));
    expect(seen).toHaveBeenCalledTimes(1);

    unreg();
    // No mounted instances at all → inert again.
    window.dispatchEvent(new KeyboardEvent('keydown', { key: ' ' }));
    expect(seen).toHaveBeenCalledTimes(1);
    asRenderer(() => window.removeEventListener('keydown', seen));
  });

  it('removeEventListener with the original reference unhooks the wrapper', () => {
    registerRendererModule(MOD);
    const unreg = registerRendererInstance(MOD, () => true);
    const seen = vi.fn();
    asRenderer(() => window.addEventListener('keydown', seen));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'a' }));
    expect(seen).toHaveBeenCalledTimes(1);

    window.removeEventListener('keydown', seen);
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'a' }));
    expect(seen).toHaveBeenCalledTimes(1);
    unreg();
  });

  it('leaves non-input events and unattributed listeners untouched', () => {
    registerRendererModule(MOD);
    const unreg = registerRendererInstance(MOD, () => false);

    const resize = vi.fn();
    asRenderer(() => window.addEventListener('resize', resize));
    window.dispatchEvent(new Event('resize'));
    expect(resize).toHaveBeenCalledTimes(1); // non-input: never gated
    window.removeEventListener('resize', resize);

    const appListener = vi.fn();
    window.addEventListener('keydown', appListener); // no attribution → app code
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'b' }));
    expect(appListener).toHaveBeenCalledTimes(1);
    window.removeEventListener('keydown', appListener);
    unreg();
  });

  it('gates on-property handler assignments from a renderer', () => {
    registerRendererModule(MOD);
    let focused = false;
    const unreg = registerRendererInstance(MOD, () => focused);
    try {
      const seen = vi.fn();
      asRenderer(() => { (window as Window).onkeydown = seen; });

      // vitest's jsdom global proxy doesn't route on*-dispatch through the
      // instance slot the way browsers do, so validate the layer directly:
      // the stored handler must be OUR gate, and the gate must consult focus.
      const stored = window.onkeydown as unknown as (ev: Event) => void;
      expect(typeof stored).toBe('function');
      expect(stored).not.toBe(seen);

      stored.call(window, new KeyboardEvent('keydown', { key: 'x' }));
      expect(seen).not.toHaveBeenCalled();

      focused = true;
      stored.call(window, new KeyboardEvent('keydown', { key: 'x' }));
      expect(seen).toHaveBeenCalledTimes(1);

      (window as Window).onkeydown = null;
    } finally {
      unreg();
    }
  });

  it('an event listener object (handleEvent) is gated the same way', () => {
    registerRendererModule(MOD);
    let focused = false;
    const unreg = registerRendererInstance(MOD, () => focused);
    const handleEvent = vi.fn();
    const obj = { handleEvent };
    asRenderer(() => window.addEventListener('pointerdown', obj));

    window.dispatchEvent(new Event('pointerdown'));
    expect(handleEvent).not.toHaveBeenCalled();
    focused = true;
    window.dispatchEvent(new Event('pointerdown'));
    expect(handleEvent).toHaveBeenCalledTimes(1);

    window.removeEventListener('pointerdown', obj);
    unreg();
  });
});

it('keeps a once listener armed until its first focused invocation __F0687', () => {
  registerRendererModule(MOD);
  let focused = false;
  const off = registerRendererInstance(MOD, () => focused);
  const seen = vi.fn();
  asRenderer(() => window.addEventListener('keydown', seen, { once: true }));
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'x' }));
  expect(seen).not.toHaveBeenCalled();
  focused = true;
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'x' }));
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'x' }));
  expect(seen).toHaveBeenCalledTimes(1);
  window.removeEventListener('keydown', seen);
  off();
});


it('keeps once, explicit removal and abort ownership separate per target __F0687',()=>{
  registerRendererModule(MOD);const off=registerRendererInstance(MOD,()=>true);
  const a=new EventTarget(),b=new EventTarget();const seen=vi.fn();
  const abort=new AbortController();
  try {
    asRenderer(()=>a.addEventListener('keydown',seen,{once:true,signal:abort.signal}));
    asRenderer(()=>b.addEventListener('keydown',seen,{once:true}));
    abort.abort();
    asRenderer(()=>a.addEventListener('keydown',seen));
    b.dispatchEvent(new Event('keydown'));b.dispatchEvent(new Event('keydown'));
    a.dispatchEvent(new Event('keydown'));a.dispatchEvent(new Event('keydown'));
    expect(seen).toHaveBeenCalledTimes(3);
    a.removeEventListener('keydown',seen);a.dispatchEvent(new Event('keydown'));
    expect(seen).toHaveBeenCalledTimes(3);
  }finally{a.removeEventListener('keydown',seen);b.removeEventListener('keydown',seen);off();}
});

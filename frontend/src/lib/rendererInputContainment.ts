/**
 * Renderer input containment — HOST-ENFORCED, not cooperative.
 *
 * Native renderers execute in the main document (shadow DOM keeps their
 * styles scoped, not their JS), so a `window.addEventListener('keydown', …)`
 * inside a renderer bundle would hear — and could preventDefault — every
 * keystroke in the app, focused cell or not. The cooperative gate
 * (api.getFocused / useHostFocus) asks renderers to behave; this module
 * makes the default the rule: input-event listeners REGISTERED FROM A
 * RENDERER MODULE are force-wrapped so they are inert while no mounted
 * instance of that renderer holds the canvas focus.
 *
 * Mechanism:
 *  - install() runs at app boot, BEFORE any renderer module can load, and
 *    patches EventTarget.prototype.add/removeEventListener plus the
 *    window/document `on<input>` handler properties.
 *  - Every native renderer module is imported from a unique URL (a blob:
 *    URL for transpiled .tsx, or the /api/renderers/<id>/source URL for
 *    pre-compiled .js). NativeRenderer registers those URLs here, plus a
 *    focus getter per mounted instance.
 *  - At registration time the patch attributes the call by scanning the
 *    stack for a registered module URL. Renderer-attributed listeners for
 *    input-type events get the focus wrapper; everything else (app code,
 *    non-input events like resize/storage/message) passes through
 *    untouched.
 *
 * Scope of the guarantee: this closes every straightforward path a
 *
 * renderer has to global input — direct calls, calls from effects, timers,
 * promises, and `on*` assignments — because the callback frames still name
 * the module URL. It is containment inside a shared realm, not a security
 * boundary: same-document code that goes out of its way to launder a
 * registration through host-realm primitives can still evade attribution.
 * Renderers that must be treated as UNTRUSTED belong in the sandboxed
 * iframe tier (`embed: "iframe"`), where the browser scopes input for real.
 */

// Event types a renderer must not observe without focus. Deliberately broad:
// keyboard, pointer/mouse/touch, wheel, clipboard, drag, text composition.
// Non-input signals (resize, storage, message, visibilitychange, pagehide…)
// stay observable — they carry no user input.
const INPUT_EVENT_TYPES = new Set([
  'keydown', 'keyup', 'keypress',
  'input', 'beforeinput',
  'compositionstart', 'compositionupdate', 'compositionend',
  'mousedown', 'mouseup', 'mousemove', 'mouseenter', 'mouseleave',
  'mouseover', 'mouseout', 'click', 'dblclick', 'auxclick', 'contextmenu',
  'pointerdown', 'pointerup', 'pointermove', 'pointerenter', 'pointerleave',
  'pointerover', 'pointerout', 'pointercancel',
  'touchstart', 'touchend', 'touchmove', 'touchcancel',
  'wheel',
  'cut', 'copy', 'paste',
  'dragstart', 'drag', 'dragenter', 'dragleave', 'dragover', 'drop', 'dragend',
]);

type FocusGetter = () => boolean;

// moduleUrl → focus getters of the currently mounted instances.
const instancesByModule = new Map<string, Set<FocusGetter>>();
// Every URL ever registered as a renderer module (attribution universe).
const moduleUrls = new Set<string>();

// original listener → (type|capture → wrapped), so removeEventListener with
// the original reference still unhooks the wrapper we actually registered.
const wrappedListeners = new WeakMap<EventTarget, WeakMap<object, Map<string, EventListener>>>();
function listenerMap(target: EventTarget, listener: object): Map<string, EventListener> {
  let listeners=wrappedListeners.get(target);
  if(!listeners){listeners=new WeakMap();wrappedListeners.set(target,listeners);}
  let entries=listeners.get(listener);
  if(!entries){entries=new Map();listeners.set(listener,entries);}
  return entries;
}

let installed = false;

// Attribution is injectable so unit tests (whose stacks contain no blob
// URLs) can simulate renderer-originated registrations.
let attribute: () => string | null = stackAttribution;

function stackAttribution(): string | null {
  if (moduleUrls.size === 0) return null;
  const stack = new Error().stack || '';
  for (const url of moduleUrls) {
    if (stack.includes(url)) return url;
  }
  return null;
}

function moduleHasFocus(moduleUrl: string): boolean {
  const getters = instancesByModule.get(moduleUrl);
  if (!getters || getters.size === 0) return false;
  for (const g of getters) {
    try { if (g()) return true; } catch { /* a broken getter never grants focus */ }
  }
  return false;
}

/** Register a URL as a renderer module (call as soon as the module loads). */
export function registerRendererModule(moduleUrl: string): void {
  if (moduleUrl) moduleUrls.add(moduleUrl);
}

/**
 * Register a mounted instance's focus getter. Returns the unregister fn —
 * NativeRenderer calls it from the mount effect's cleanup.
 */
export function registerRendererInstance(moduleUrl: string, getFocused: FocusGetter): () => void {
  if (!moduleUrl) return () => {};
  moduleUrls.add(moduleUrl);
  let set = instancesByModule.get(moduleUrl);
  if (!set) { set = new Set(); instancesByModule.set(moduleUrl, set); }
  set.add(getFocused);
  return () => {
    const s = instancesByModule.get(moduleUrl);
    if (s) { s.delete(getFocused); if (s.size === 0) instancesByModule.delete(moduleUrl); }
  };
}

function wrapKey(type: string, capture: boolean): string {
  return `${type}|${capture ? 1 : 0}`;
}

function getOrCreateWrapper(
  target: EventTarget,
  moduleUrl: string,
  type: string,
  capture: boolean,
  listener: EventListenerOrEventListenerObject,
  once: boolean,
): EventListener {
  const keyable = listener as object;
  const byKey = listenerMap(target,keyable);
  const key = wrapKey(type, capture);
  const existing = byKey.get(key);
  if (existing) return existing;
  const wrapped: EventListener = function (this: unknown, ev: Event) {
    if (!moduleHasFocus(moduleUrl)) return;
    if (once) {
      (this as EventTarget).removeEventListener(type, wrapped, capture);
      byKey!.delete(key);
    }
    if (typeof listener === 'function') return (listener as EventListener).call(this, ev);
    return (listener as EventListenerObject).handleEvent(ev);
  };
  byKey.set(key, wrapped);
  return wrapped;
}

function lookupWrapper(
  target: EventTarget,
  type: string,
  capture: boolean,
  listener: EventListenerOrEventListenerObject,
): EventListener | null {
  const byKey = wrappedListeners.get(target)?.get(listener as object);
  return byKey?.get(wrapKey(type, capture)) ?? null;
}

function captureFlag(options?: boolean | AddEventListenerOptions): boolean {
  return typeof options === 'boolean' ? options : !!options?.capture;
}

/**
 * Patch the event surfaces. Must run before any renderer module is imported;
 * idempotent so hot reload / duplicate imports can't double-wrap.
 */
export function installRendererInputContainment(): void {
  if (installed || typeof window === 'undefined') return;
  installed = true;

  const makeAdd = (nativeAdd: typeof EventTarget.prototype.addEventListener) =>
    function (
      this: EventTarget,
      type: string,
      listener: EventListenerOrEventListenerObject | null,
      options?: boolean | AddEventListenerOptions,
    ) {
      if (listener && INPUT_EVENT_TYPES.has(type)) {
        const moduleUrl = attribute();
        if (moduleUrl) {
          if(typeof options==='object' && options.signal?.aborted)return;
          const wrapped = getOrCreateWrapper(this,moduleUrl, type, captureFlag(options), listener, typeof options === 'object' && !!options.once);
          if(typeof options==='object' && options.signal) {
            const entries=listenerMap(this,listener as object);
            const key=wrapKey(type,captureFlag(options));
            options.signal.addEventListener('abort',()=>{if(entries.get(key)===wrapped)entries.delete(key);},{once:true});
          }
          return nativeAdd.call(this, type, wrapped, typeof options === 'object' ? {...options,once:false} : options);
        }
      }
      return nativeAdd.call(this, type, listener, options);
    };

  const makeRemove = (nativeRemove: typeof EventTarget.prototype.removeEventListener) =>
    function (
      this: EventTarget,
      type: string,
      listener: EventListenerOrEventListenerObject | null,
      options?: boolean | EventListenerOptions,
    ) {
      if (listener && INPUT_EVENT_TYPES.has(type)) {
        const wrapped = lookupWrapper(this,type, captureFlag(options), listener);
        if (wrapped) {
          nativeRemove.call(this, type, wrapped, options);
          wrappedListeners.get(this)?.get(listener as object)?.delete(wrapKey(type,captureFlag(options)));
        }
      }
      return nativeRemove.call(this, type, listener, options);
    };

  const proto = EventTarget.prototype;
  const patchedAdd = makeAdd(proto.addEventListener);
  const patchedRemove = makeRemove(proto.removeEventListener);
  proto.addEventListener = patchedAdd;
  proto.removeEventListener = patchedRemove;

  // Some hosts (jsdom; browsers with polyfills) expose BOUND own
  // add/removeEventListener functions on window/document that skip the
  // prototype — wrap those instances too so no registration path bypasses
  // the gate.
  for (const target of [window, document] as const) {
    if (target.addEventListener !== patchedAdd) {
      const ownAdd = target.addEventListener.bind(target);
      const ownRemove = target.removeEventListener.bind(target);
      try {
        Object.defineProperty(target, 'addEventListener', {
          configurable: true, writable: true, value: makeAdd(ownAdd),
        });
        Object.defineProperty(target, 'removeEventListener', {
          configurable: true, writable: true, value: makeRemove(ownRemove),
        });
      } catch { /* sealed host object: prototype patch still covers it */ }
    }
  }

  // `window.onkeydown = fn` style assignments — same attribution, same
  // wrapper, applied to the two global surfaces a renderer could reach.
  // The override delegates to the ORIGINAL accessor (found on the prototype
  // chain) so the native handler slot keeps driving dispatch; we only
  // substitute the focus wrapper on the way in.
  const findNativeAccessor = (target: object, prop: string) => {
    let o: object | null = target;
    while (o) {
      const d = Object.getOwnPropertyDescriptor(o, prop);
      if (d && (d.get || d.set)) return d;
      o = Object.getPrototypeOf(o);
    }
    return null;
  };
  for (const target of [window, document] as const) {
    for (const type of INPUT_EVENT_TYPES) {
      const prop = `on${type}`;
      if (!(prop in target)) continue;
      const nativeDesc = findNativeAccessor(target, prop);
      if (!nativeDesc?.set || !nativeDesc.get) continue;
      const nativeGet = nativeDesc.get;
      const nativeSet = nativeDesc.set;
      try {
        Object.defineProperty(target, prop, {
          configurable: true,
          get() { return nativeGet.call(target); },
          set(fn: unknown) {
            const moduleUrl = typeof fn === 'function' ? attribute() : null;
            if (moduleUrl && typeof fn === 'function') {
              nativeSet.call(target, function (this: unknown, ev: Event) {
                if (!moduleHasFocus(moduleUrl)) return;
                return (fn as EventListener).call(this, ev);
              });
            } else {
              nativeSet.call(target, fn);
            }
          },
        });
      } catch { /* a host that seals these properties keeps native behavior */ }
    }
  }
}

/** Test hook: replace stack attribution (pass null to restore the default). */
export function _setAttributorForTests(fn: (() => string | null) | null): void {
  attribute = fn ?? stackAttribution;
}

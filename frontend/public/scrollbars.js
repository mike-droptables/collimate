// Generated from src/lib/scrollbars.ts by scripts/build-scrollbars.mjs; do not edit.
// src/lib/scrollbars.ts
var MIN_THUMB_PX = 24;
var SCROLLBAR_SIZE_PX = 16;
var SCROLLBAR_FADE_IN_MS = 180;
var SCROLLBAR_FADE_OUT_MS = 420;
var SCROLL_IDLE_MS = 2500;
var SCROLLING_ATTR = "data-scrollbar-active";
var DRAGGING_ATTR = "data-scrollbar-dragging";
var SCROLLBAR_CSS = `
@property --collimate-scrollbar-opacity { syntax: '<percentage>'; inherits: true; initial-value: 0%; }
* { --collimate-scrollbar-opacity: 0%; }
[${SCROLLING_ATTR}] { --collimate-scrollbar-opacity: 100%; }
[${DRAGGING_ATTR}] { --collimate-scrollbar-opacity: 100%; }
::-webkit-scrollbar { width: ${SCROLLBAR_SIZE_PX}px; height: ${SCROLLBAR_SIZE_PX}px; }
::-webkit-scrollbar-track, ::-webkit-scrollbar-corner { background: transparent; }
::-webkit-scrollbar-thumb { background-color: color-mix(in srgb, var(--line-2) var(--collimate-scrollbar-opacity), transparent); border: 4px solid transparent; border-radius: 8px; background-clip: padding-box; min-height: ${MIN_THUMB_PX}px; min-width: ${MIN_THUMB_PX}px; }
::-webkit-scrollbar-thumb:hover { background-color: color-mix(in srgb, var(--ink-3) var(--collimate-scrollbar-opacity), transparent); }
::-webkit-scrollbar-thumb:active, [${DRAGGING_ATTR}]::-webkit-scrollbar-thumb { background-color: color-mix(in srgb, var(--ink-2) var(--collimate-scrollbar-opacity), transparent); }
@supports not selector(::-webkit-scrollbar) { * { scrollbar-width: auto; scrollbar-color: color-mix(in srgb, var(--line-2) var(--collimate-scrollbar-opacity), transparent) transparent; } }
`;
var PAGE_FRACTION = 0.875;
function barred(overflow, scroll, client) {
  return (overflow === "auto" || overflow === "scroll" || overflow === "overlay") && scroll > client;
}
function thumbOn(trackStart, trackLength, client, scroll, position, minThumb) {
  const maxScroll = Math.max(0, scroll - client);
  const thumbLength = Math.min(trackLength, Math.max(minThumb, trackLength * client / scroll));
  const room = trackLength - thumbLength;
  const thumbStart = trackStart + (maxScroll > 0 && room > 0 ? room * Math.min(Math.max(position, 0), maxScroll) / maxScroll : 0);
  return [thumbStart, thumbLength, maxScroll];
}
function guttersOf(m, minThumb = MIN_THUMB_PX) {
  const out = {};
  const roomRight = m.offsetWidth - m.clientLeft - m.clientWidth - m.borderRight;
  const roomBottom = m.offsetHeight - m.clientTop - m.clientHeight - m.borderBottom;
  if (barred(m.overflowY, m.scrollHeight, m.clientHeight)) {
    const gutterWidth = roomRight > 0 ? roomRight : m.barWidth;
    if (gutterWidth > 0) {
      const trackStart = m.top + m.clientTop;
      const [thumbStart, thumbLength, maxScroll] = thumbOn(trackStart, m.clientHeight, m.clientHeight, m.scrollHeight, m.scrollTop, minThumb);
      const bandEnd = m.left + m.offsetWidth - m.borderRight;
      out.y = { axis: "y", trackStart, trackLength: m.clientHeight, thumbStart, thumbLength, maxScroll, scroll: m.scrollTop, band: [bandEnd - gutterWidth, bandEnd] };
    }
  }
  if (barred(m.overflowX, m.scrollWidth, m.clientWidth)) {
    const gutterHeight = roomBottom > 0 ? roomBottom : m.barHeight;
    if (gutterHeight > 0) {
      const trackStart = m.left + m.clientLeft;
      const [thumbStart, thumbLength, maxScroll] = thumbOn(trackStart, m.clientWidth, m.clientWidth, m.scrollWidth, m.scrollLeft, minThumb);
      const bandEnd = m.top + m.offsetHeight - m.borderBottom;
      out.x = { axis: "x", trackStart, trackLength: m.clientWidth, thumbStart, thumbLength, maxScroll, scroll: m.scrollLeft, band: [bandEnd - gutterHeight, bandEnd] };
    }
  }
  return out;
}
function gutterAt(m, px, py, minThumb = MIN_THUMB_PX) {
  const { x, y } = guttersOf(m, minThumb);
  const within = (g, across, along) => across >= g.band[0] && across < g.band[1] && along >= g.trackStart && along < g.trackStart + g.trackLength;
  if (y && within(y, px, py)) return { gutter: y, onThumb: py >= y.thumbStart && py < y.thumbStart + y.thumbLength, along: py };
  if (x && within(x, py, px)) return { gutter: x, onThumb: px >= x.thumbStart && px < x.thumbStart + x.thumbLength, along: px };
  return null;
}
function dragScroll(g, along, grab) {
  const room = g.trackLength - g.thumbLength;
  if (room <= 0 || g.maxScroll <= 0) return g.scroll;
  const start = along - grab - g.trackStart;
  return Math.min(g.maxScroll, Math.max(0, start / room * g.maxScroll));
}
function metricsOf(el) {
  const rect = el.getBoundingClientRect();
  const style = getComputedStyle(el);
  const bar = getComputedStyle(el, "::-webkit-scrollbar");
  return {
    left: rect.left,
    top: rect.top,
    offsetWidth: el.offsetWidth,
    offsetHeight: el.offsetHeight,
    clientLeft: el.clientLeft,
    clientTop: el.clientTop,
    borderRight: parseFloat(style.borderRightWidth) || 0,
    borderBottom: parseFloat(style.borderBottomWidth) || 0,
    clientWidth: el.clientWidth,
    clientHeight: el.clientHeight,
    scrollWidth: el.scrollWidth,
    scrollHeight: el.scrollHeight,
    scrollLeft: el.scrollLeft,
    scrollTop: el.scrollTop,
    overflowX: style.overflowX,
    overflowY: style.overflowY,
    barWidth: parseFloat(bar.width) || 0,
    barHeight: parseFloat(bar.height) || 0
  };
}
function scrollTargetOf(event, root) {
  const first = event.composedPath()[0];
  if (first instanceof Element) return first;
  if (first instanceof Document) return first.documentElement;
  return root instanceof Document ? root.documentElement : null;
}
function installScrollActivity(root = document, idleMs = SCROLL_IDLE_MS) {
  const active = /* @__PURE__ */ new Map();
  const animate = (el, state, visible) => {
    const before = getComputedStyle(el).getPropertyValue("--collimate-scrollbar-opacity").trim() || "0%";
    state.animation?.cancel();
    el.toggleAttribute(SCROLLING_ATTR, visible);
    const after = visible ? "100%" : "0%";
    if (before === after || typeof el.animate !== "function") {
      state.animation = null;
      if (!visible) active.delete(el);
      return;
    }
    const animation = el.animate(
      [{ "--collimate-scrollbar-opacity": before }, { "--collimate-scrollbar-opacity": after }],
      { duration: visible ? SCROLLBAR_FADE_IN_MS : SCROLLBAR_FADE_OUT_MS, easing: "ease-out" }
    );
    state.animation = animation;
    animation.onfinish = () => {
      if (state.animation !== animation) return;
      animation.cancel();
      state.animation = null;
      if (!el.hasAttribute(SCROLLING_ATTR)) active.delete(el);
    };
  };
  const reveal = (el) => {
    let state = active.get(el);
    if (!state) {
      state = { timer: null, animation: null };
      active.set(el, state);
    }
    if (!el.hasAttribute(SCROLLING_ATTR)) animate(el, state, true);
    if (state.timer !== null) clearTimeout(state.timer);
    state.timer = setTimeout(() => {
      state.timer = null;
      if (!el.hasAttribute(DRAGGING_ATTR)) animate(el, state, false);
    }, idleMs);
  };
  const onScroll = (event) => {
    const el = scrollTargetOf(event, root);
    if (el) reveal(el);
  };
  const pointerScroller = (event) => {
    const first = event.composedPath()[0];
    if (!(first instanceof Element) || first.getRootNode() !== root) return null;
    for (let el = first; el; el = el.parentElement) {
      if (!(el instanceof HTMLElement)) continue;
      const style = getComputedStyle(el);
      if (barred(style.overflowY, el.scrollHeight, el.clientHeight) || barred(style.overflowX, el.scrollWidth, el.clientWidth)) return el;
      if (root instanceof ShadowRoot && el === root.host) break;
    }
    if (root instanceof Document) {
      const page = root.scrollingElement;
      if (page && (page.scrollHeight > page.clientHeight || page.scrollWidth > page.clientWidth)) return page;
    }
    return null;
  };
  const onPointerOver = (event) => {
    if (event.pointerType === "touch") return;
    const el = pointerScroller(event);
    if (el) reveal(el);
  };
  const onPointerEnd = (event) => {
    const el = pointerScroller(event);
    if (el?.hasAttribute(DRAGGING_ATTR)) reveal(el);
  };
  let lastPoint = null;
  const onPointerMove = (event) => {
    if (event.pointerType === "touch") return;
    if (lastPoint?.id === event.pointerId && lastPoint.x === event.clientX && lastPoint.y === event.clientY) return;
    lastPoint = { id: event.pointerId, x: event.clientX, y: event.clientY };
    const el = pointerScroller(event);
    if (el) reveal(el);
  };
  root.addEventListener("scroll", onScroll, { capture: true, passive: true });
  root.addEventListener("pointerover", onPointerOver, { capture: true, passive: true });
  root.addEventListener("pointerdown", onPointerOver, { capture: true, passive: true });
  root.addEventListener("pointermove", onPointerMove, { capture: true, passive: true });
  root.addEventListener("pointerup", onPointerEnd, { capture: true, passive: true });
  root.addEventListener("pointercancel", onPointerEnd, { capture: true, passive: true });
  return () => {
    root.removeEventListener("scroll", onScroll, { capture: true });
    root.removeEventListener("pointerover", onPointerOver, { capture: true });
    root.removeEventListener("pointerdown", onPointerOver, { capture: true });
    root.removeEventListener("pointermove", onPointerMove, { capture: true });
    root.removeEventListener("pointerup", onPointerEnd, { capture: true });
    root.removeEventListener("pointercancel", onPointerEnd, { capture: true });
    for (const [el, state] of active) {
      if (state.timer !== null) clearTimeout(state.timer);
      state.animation?.cancel();
      el.removeAttribute(SCROLLING_ATTR);
    }
    active.clear();
  };
}
function gutterPressed(event) {
  const first = event.composedPath()[0];
  if (!(first instanceof HTMLElement)) return null;
  const doc = first.ownerDocument;
  for (let el = first; el && el !== doc.documentElement && el !== doc.body; el = el.parentElement) {
    if (getComputedStyle(el).scrollbarWidth === "none") continue;
    const hit = gutterAt(metricsOf(el), event.clientX, event.clientY);
    if (hit) return { el, hit };
  }
  return null;
}
function scrollTo(el, axis, value) {
  el.scrollTo({ [axis === "y" ? "top" : "left"]: value, behavior: "instant" });
}
function installThumbDrag(root = document) {
  let drag = null;
  const end = () => {
    if (!drag) return;
    const { el, pointerId } = drag;
    drag = null;
    el.removeAttribute(DRAGGING_ATTR);
    try {
      el.releasePointerCapture(pointerId);
    } catch {
    }
  };
  const onPointerDown = (event) => {
    if (event.button !== 0 || event.pointerType === "touch") return;
    const pressed = gutterPressed(event);
    if (!pressed) return;
    const { el, hit } = pressed;
    event.preventDefault();
    const { gutter } = hit;
    if (!hit.onThumb) {
      const page = gutter.trackLength * PAGE_FRACTION;
      const next = gutter.scroll + (hit.along < gutter.thumbStart ? -page : page);
      scrollTo(el, gutter.axis, Math.min(gutter.maxScroll, Math.max(0, next)));
      return;
    }
    end();
    drag = { el, pointerId: event.pointerId, gutter, grab: hit.along - gutter.thumbStart };
    el.setAttribute(DRAGGING_ATTR, "");
    try {
      el.setPointerCapture(event.pointerId);
    } catch {
    }
  };
  const onMouseDown = (event) => {
    if (gutterPressed(event)) event.preventDefault();
  };
  const onPointerMove = (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.preventDefault();
    const along = drag.gutter.axis === "y" ? event.clientY : event.clientX;
    scrollTo(drag.el, drag.gutter.axis, dragScroll(drag.gutter, along, drag.grab));
  };
  const onPointerEnd = (event) => {
    if (drag && event.pointerId === drag.pointerId) end();
  };
  const listeners = [
    ["pointerdown", onPointerDown],
    ["mousedown", onMouseDown],
    ["pointermove", onPointerMove],
    ["pointerup", onPointerEnd],
    ["pointercancel", onPointerEnd]
  ];
  for (const [type, listener] of listeners) root.addEventListener(type, listener, { capture: true });
  return () => {
    end();
    for (const [type, listener] of listeners) root.removeEventListener(type, listener, { capture: true });
  };
}
function installScrollbars(root = document) {
  const undoActivity = installScrollActivity(root);
  const undoDrag = installThumbDrag(root);
  return () => {
    undoDrag();
    undoActivity();
  };
}
var sheet;
function scrollbarSheet() {
  if (sheet !== void 0) return sheet;
  try {
    const made = new CSSStyleSheet();
    made.replaceSync(SCROLLBAR_CSS);
    sheet = made;
  } catch {
    sheet = null;
  }
  return sheet;
}
function adoptScrollbars(root) {
  const made = scrollbarSheet();
  if (!made || !("adoptedStyleSheets" in root)) return;
  if (!root.adoptedStyleSheets.includes(made)) root.adoptedStyleSheets = [...root.adoptedStyleSheets, made];
}

// src/lib/scrollbars.document.ts
adoptScrollbars(document);
installScrollbars(document);

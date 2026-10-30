// SPDX-License-Identifier: MIT
/**
 * The document view: how a renderer shows an HTML document someone else
 * authored, in an opaque srcdoc frame of its own. The html renderer shows
 * every `.html` page through it; any renderer that previews an authored page
 * can do the same, so a file is shown, darkened and recoloured one way.
 *
 * THE PAGE IS THE AUTHOR'S. No frame is drawn around it (no margin, no
 * hairline, no radius): the page fills the renderer's box, and no skin
 * colour or typography is injected into it. Each renderer keeps its own
 * prepare step (what it does to the markup before it is shown: the html
 * renderer's sibling-ref rewrite, `<base>` and link bridge) and its own
 * `<iframe>` with its own `sandbox` literal; this module owns everything
 * between: the dark mode, the frame's backing, the host-theme gate and the
 * element picker.
 *
 * DARK MODE is DarkReader injected INTO the page, not a filter over the
 * frame. `filter: invert(1) hue-rotate(180deg)` inverts photos, video,
 * canvas and syntax-highlighted code along with the prose, and cannot tell an
 * already-dark element from a light one. DarkReader runs inside the page's
 * own realm and reads the styles it recolours. It rides in the srcDoc at the
 * START of `<head>` (after a leading Content-Security-Policy meta, which
 * must govern it) so it paints before first layout. The library is served
 * from the app's own origin (`DARKREADER_PATH`), pinned, so a page darkens
 * offline and a skin never depends on a CDN. It darkens the page ONTO THE
 * SKIN: its dark ground and text are the skin's own colours
 * (`darkReaderOptions`), and a skin change re-enables it inside the loaded
 * page with the new colours (a message, not a reload).
 *
 * THE VARIABLES BLOCK. A renderer may give its page a `:root` block of its
 * own (`variables`: the html renderer's text-size zoom). The view keeps it
 * in a `<style>` of its own at the front of the head, so the first paint
 * has it, and a change replaces its text inside the loaded page with the
 * same message DarkReader's recolour rides on. The srcDoc bakes the skin
 * current when it is built and never depends on the skin after that: a
 * skin change recolours the page in place, never reloads it.
 *
 * PRECEDENCE for "is this page dark": the caller's `dark` wins when it is a
 * boolean, otherwise the skin's scheme. The caller takes it from the user's
 * choice in its style control (`useStyle(api).theme`: `'darkened'` true,
 * `'authored'` false, null follows the skin), which the host keeps per
 * channel under the renderer's id, so each renderer has its own. A change
 * rebuilds the srcDoc, which reloads the page: the same swap a
 * `live_refresh` write performs, and injecting DarkReader after load would
 * flash the light page every time.
 *
 * The srcDoc is not built until the host's theme is KNOWN, whatever the
 * choice: the iframe host's placeholder reads `'dark'` whatever the skin, and
 * a darkened page bakes the skin's colours. Building from the guess would
 * load the user's page twice (see `hostThemeKnown`).
 *
 * THE POINTER REPORT. The frame takes every pointer move over the page, so
 * the renderer's document never sees one there. A view asked to
 * (`reportPointer`) gives the page a few lines in its bootstrap that post
 * the pointer's position in the page's viewport to the parent window
 * (`POINTER_MESSAGE`), at most once an animation frame and always the
 * latest, so a `StyleControl` floating over the page (its `frame`) knows
 * when the pointer comes near its corner without covering any of the page.
 *
 * THE ELEMENT PICKER. A view asked to (`pickElements`) gives the page a
 * small bridge (`PICK_BRIDGE_JS`) in the same bootstrap, ahead of
 * DarkReader's part. Idle, it does nothing but answer a probe. Armed by the
 * view (`pick(signal, hint?)`), it outlines the element under the pointer
 * in a box drawn inside a closed shadow root of its own (the skin's
 * `--brass`, so the page's styles and DarkReader never reach it), shows the
 * hint, and stops every press, click and key in the capture phase so the
 * page's own handlers never see them. A primary click posts the element
 * (`collimate:picked`: tag, CSS path, the nearest id, a text snippet and a
 * markup excerpt) and flashes it once; Esc posts `collimate:pick-abandoned`.
 * The bridge announces itself (`collimate:pick-ready`) when it starts and
 * whenever the view probes it, which the view does on every load: a pick
 * pending across a reload re-arms in the new page, and a page that never
 * answers (it navigated away, a policy refused the script) rejects the pick
 * after `PICK_READY_WAIT_MS`.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { resolveVar, toHex8 } from './color';
import type { RendererTheme, Scheme } from './types';

// ── the library ───────────────────────────────────────────────────────────

/** DarkReader, PINNED: the app serves exactly this build (its licence beside
 *  it). Changing the version means vendoring the new build at the new path. */
export const DARKREADER_VERSION = '4.9.133';

/** Where the app serves the pinned DarkReader build, on its own origin. */
export const DARKREADER_PATH = `/darkreader/${DARKREADER_VERSION}/darkreader.js`;

/** The library's absolute URL on the origin this renderer was served from.
 *  Absolute because the srcdoc page is an opaque origin with no usable base
 *  (the html renderer points `<base>` at the cell). `location.href`, never
 *  `location.origin`, as the base: an opaque origin reads 'null'. Empty when
 *  there is no http(s) location (a test, a file: page): then nothing is
 *  injected and the page shows as authored. */
export function darkReaderScriptUrl(): string {
    try {
        const url = new URL(DARKREADER_PATH, location.href);
        return /^https?:$/.test(url.protocol) ? url.href : '';
    } catch {
        return '';
    }
}

// ── DarkReader on the skin ────────────────────────────────────────────────

/** The skin colours DarkReader darkens a page onto, as its `enable(theme)`
 *  options. Only these two: its defaults for everything else (brightness 100,
 *  contrast 100, sepia 0) leave the computed colours untouched, so the page's
 *  ground is exactly the skin colour the backing paints. */
export interface DarkReaderOptions {
    darkSchemeBackgroundColor?: string;
    darkSchemeTextColor?: string;
}

/** The skin tokens a darkened page takes as ground and text. In a dark skin,
 *  `--sheet` under `--ink`. A light skin's `--sheet` is light, so there the
 *  pair swaps: the same two colours, inverted. */
export function darkReaderPoles(scheme: Scheme): { ground: '--sheet' | '--ink'; text: '--sheet' | '--ink' } {
    return scheme === 'light'
        ? { ground: '--ink', text: '--sheet' }
        : { ground: '--sheet', text: '--ink' };
}

type ThemeLike = Pick<RendererTheme, 'scheme' | 'vars'> & { known?: boolean };

/** DarkReader's options for a skin snapshot, resolved to `#rrggbbaa`. A token
 *  the snapshot lacks is left out, and DarkReader uses its own default. */
export function darkReaderOptions(theme: ThemeLike | null | undefined): DarkReaderOptions {
    const record = (theme || { scheme: 'dark', vars: {} }) as RendererTheme;
    const poles = darkReaderPoles(record.scheme === 'light' ? 'light' : 'dark');
    const ground = toHex8(resolveVar(record, poles.ground));
    const text = toHex8(resolveVar(record, poles.text));
    const out: DarkReaderOptions = {};
    if (ground) out.darkSchemeBackgroundColor = ground;
    if (text) out.darkSchemeTextColor = text;
    return out;
}

/** The message a view posts into its page when the skin changes: the
 *  page's listener writes the variables block it carries onto the view's
 *  `<style>` (`DOCUMENT_VARIABLES_ID`) and re-enables DarkReader, in a
 *  darkened page, with the options it carries. */
export const DARKREADER_MESSAGE = 'collimate:darkreader';

/** The message a page told to (`reportPointer`) posts to the parent window
 *  as the pointer moves over it: `{ type, x, y, w, h }`, the pointer's
 *  position in the page's viewport and that viewport's size, CSS px. The
 *  `StyleControl` floating over the frame (its `frame`) reads it. */
export const POINTER_MESSAGE = 'collimate:pointer';

/** The id of the `<style>` holding a page's variables block. */
export const DOCUMENT_VARIABLES_ID = 'collimate-document-variables';

/** What the injected tags need from the view: the library's URL and the
 *  nonce a page's own CSP admits scripts by. */
export interface DarkReaderInjection {
    src: string;
    nonce?: string;
}

const NONCE = /^[A-Za-z0-9+/=_-]+$/;

/** A listener's first line: only the parent window, only `DARKREADER_MESSAGE`. */
const FROM_HOST = `var d=e.data;if(e.source!==window.parent||!d||d.type!=="${DARKREADER_MESSAGE}")return;`;

// ── the element picker ────────────────────────────────────────────────────

/** The messages between a view and its page's picker: the view posts
 *  `start` (`{ id, hint, colors }`), `cancel` (`{ id }`) and `probe`; the
 *  page posts `ready`, `picked` (`{ id, tag, css, elementId, snippet, html }`)
 *  and `abandoned` (`{ id }`, the user pressed Esc). */
export const PICK_MESSAGE = Object.freeze({
    start: 'collimate:pick-start', cancel: 'collimate:pick-cancel', probe: 'collimate:pick-probe',
    ready: 'collimate:pick-ready', picked: 'collimate:picked', abandoned: 'collimate:pick-abandoned',
} as const);

/** How long a pending pick waits, after a load, for the page's picker to
 *  answer before it rejects (`PICK_UNREACHABLE`). */
export const PICK_READY_WAIT_MS = 3000;

/** The element a pick resolves to: its tag, its CSS path from the nearest
 *  id (or `body`), that id (the element's own or its nearest ancestor's,
 *  else ''), a text snippet (at most 120 characters) and a markup excerpt
 *  (at most 600), whitespace collapsed and clipped with '…'. */
export interface ElementPick { tag: string; css: string; id: string; snippet: string; html: string }

/** The picker's colours, each `#rrggbbaa` or '' (the page then uses the
 *  system's `Highlight` colours): the outline, its fill, the hint's ground
 *  and the hint's text. */
export interface PickColors { line: string; fill: string; ground: string; ink: string }

/** The picker's colours from a skin snapshot: `--brass` for the outline and
 *  the hint's ground, `--brass-soft` for the fill, `--sheet` for the hint's
 *  text. A token the snapshot lacks reads ''. */
export function pickColors(theme: ThemeLike | null | undefined): PickColors {
    const record = (theme || { scheme: 'dark', vars: {} }) as RendererTheme;
    const hex = (token: '--brass' | '--brass-soft' | '--sheet') => toHex8(resolveVar(record, token)) || '';
    return { line: hex('--brass'), fill: hex('--brass-soft'), ground: hex('--brass'), ink: hex('--sheet') };
}

function clipped(value: unknown, max: number): string {
    if (typeof value !== 'string') return '';
    return value.length > max ? value.slice(0, max - 1) + '…' : value;
}

/** A `collimate:picked` payload as an `ElementPick`: the tag lowercased (an
 *  odd one reads 'element'), the CSS path at most 1000 characters, the id
 *  (from `elementId`) 200, the snippet 120 and the markup 600, clipped with
 *  '…'; a field that is not a string reads ''. */
export function elementPickOf(raw: unknown): ElementPick {
    const data = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
    const tag = typeof data.tag === 'string' ? data.tag.toLowerCase() : '';
    return {
        tag: /^[a-z][a-z0-9-]*$/.test(tag) ? tag : 'element',
        css: clipped(data.css, 1000),
        id: clipped(data.elementId, 200),
        snippet: clipped(data.snippet, 120),
        html: clipped(data.html, 600),
    };
}

/** What a pick rejects with when the page never answers the picker. */
export const PICK_UNREACHABLE = 'The page does not run the element picker: it navigated away from the file, or a policy refused the picker’s script.';

/** The page's picker, one function run in the page's bootstrap (see THE
 *  ELEMENT PICKER above). It names only `window`, `document` and built-ins,
 *  holds no `<` (it sits inside a `<script>`), and is raw text, so its
 *  regular expressions and '…' reach the page as written. */
export const PICK_BRIDGE_JS = String.raw`(function(){"use strict";
var M={start:"collimate:pick-start",cancel:"collimate:pick-cancel",probe:"collimate:pick-probe",ready:"collimate:pick-ready",picked:"collimate:picked",abandoned:"collimate:pick-abandoned"};
var parentWindow=window.parent,armed=null,host=null,box=null,tip=null,cursor=null,last=null,flashing=0;
function post(m){try{parentWindow.postMessage(m,"*");}catch(x){}}
function colour(v,f){return typeof v==="string"&&/^#[0-9a-f]{8}$/i.test(v)?v:f;}
function clip(v,n){var s=String(v==null?"":v).replace(/\s+/g," ").trim();return s.length>n?s.slice(0,n-1)+"…":s;}
function layer(){if(host&&host.isConnected)return;
host=document.createElement("collimate-pick");host.setAttribute("data-collimate-pick","");
host.style.cssText="position:fixed;top:0;left:0;width:0;height:0;margin:0;padding:0;border:0;overflow:visible;pointer-events:none;z-index:2147483647;display:block;";
var root=host.attachShadow({mode:"closed"});
box=document.createElement("div");box.style.cssText="position:fixed;display:none;box-sizing:border-box;pointer-events:none;border-radius:2px;";
tip=document.createElement("div");tip.style.cssText="position:fixed;top:8px;left:50%;transform:translateX(-50%);display:none;pointer-events:none;padding:3px 10px;border-radius:10px;font:12px/1.4 system-ui,sans-serif;white-space:nowrap;max-width:90vw;overflow:hidden;text-overflow:ellipsis;";
root.appendChild(box);root.appendChild(tip);document.documentElement.appendChild(host);}
function paint(c){c=c||{};box.style.border="2px solid "+colour(c.line,"Highlight");box.style.background=colour(c.fill,"transparent");tip.style.background=colour(c.ground,"Highlight");tip.style.color=colour(c.ink,"HighlightText");}
function targetOf(e){var t=e.target;if(!t||t.nodeType!==1||t===document.documentElement)t=document.body;return t&&t!==host?t:null;}
function place(el){if(!box)return;if(!el||!el.isConnected){box.style.display="none";return;}
var r=el.getBoundingClientRect();box.style.top=r.top+"px";box.style.left=r.left+"px";box.style.width=Math.max(r.width,2)+"px";box.style.height=Math.max(r.height,2)+"px";box.style.display="block";}
function ident(id){var c=window.CSS;return c&&typeof c.escape==="function"?c.escape(id):id;}
function cssPath(el){if(el===document.body)return "body";var parts=[];
while(el&&el.nodeType===1&&el!==document.body&&el!==document.documentElement){
if(el.id){parts.unshift("#"+ident(el.id));return parts.join(" > ");}
var i=1,sib=el;while((sib=sib.previousElementSibling)){if(sib.tagName===el.tagName)i++;}
parts.unshift(String(el.tagName).toLowerCase()+":nth-of-type("+i+")");el=el.parentElement;}
return parts.join(" > ");}
function swallow(e){if(!armed)return;e.preventDefault();e.stopImmediatePropagation();}
function onMove(e){if(!armed)return;last=targetOf(e);place(last);}
function onClick(e){if(!armed)return;swallow(e);if(e.button!==0)return;var el=targetOf(e);if(!el)return;
var a=armed;disarm();var withId=el.closest?el.closest("[id]"):null;
post({type:M.picked,id:a.id,tag:String(el.tagName||"").toLowerCase(),css:cssPath(el),elementId:withId?String(withId.id):"",
snippet:clip(String(el.textContent||"").slice(0,2000),120),html:clip(String(el.outerHTML||"").slice(0,4000),600)});
flash(el,a.colors);}
function onKey(e){if(!armed)return;swallow(e);if(e.type==="keydown"&&e.key==="Escape"){var id=armed.id;disarm();post({type:M.abandoned,id:id});}}
function arm(d){layer();armed={id:d.id,colors:d.colors&&typeof d.colors==="object"?d.colors:{}};paint(armed.colors);
tip.textContent=typeof d.hint==="string"&&d.hint?d.hint.slice(0,200):"Click an element to attach it · Esc cancels";tip.style.display="block";
if(!cursor){cursor=document.createElement("style");cursor.textContent="*{cursor:crosshair !important}";(document.head||document.documentElement).appendChild(cursor);}
if(last)place(last);}
function disarm(){armed=null;if(tip)tip.style.display="none";if(box&&!flashing)box.style.display="none";if(cursor){cursor.remove();cursor=null;}}
function flash(el,c){layer();paint(c);place(el);if(flashing)window.clearTimeout(flashing);flashing=window.setTimeout(function(){flashing=0;if(!armed&&box)box.style.display="none";},600);}
var capture={capture:true,passive:false};
["pointerdown","pointerup","mousedown","mouseup","dblclick","auxclick","contextmenu","touchstart","touchend","dragstart"].forEach(function(t){window.addEventListener(t,swallow,capture);});
window.addEventListener("click",onClick,capture);window.addEventListener("keydown",onKey,capture);
window.addEventListener("keyup",onKey,capture);window.addEventListener("keypress",onKey,capture);
window.addEventListener("pointermove",onMove,{capture:true,passive:true});
window.addEventListener("scroll",function(){if(armed&&last)place(last);},{capture:true,passive:true});
window.addEventListener("message",function(e){if(e.source!==parentWindow)return;var d=e.data;if(!d||typeof d.type!=="string")return;
if(d.type===M.probe){post({type:M.ready});return;}
if(d.type===M.start&&typeof d.id==="string"&&d.id){arm(d);return;}
if(d.type===M.cancel&&armed&&d.id===armed.id)disarm();});
post({type:M.ready});
})();`;

// ── the srcDoc ────────────────────────────────────────────────────────────

/** What the view puts at the front of the head: the library (darkened
 *  only), the variables block's `<style>` (a view with one only), then one
 *  bootstrap that listens for the next skin. The variables listener comes
 *  first, so a recolour re-enables DarkReader over the new block, then the
 *  pointer report (`pointer`, `POINTER_MESSAGE`, in a try of its own), then
 *  the element picker (`pick`, `PICK_BRIDGE_JS`, in a try of its own), then
 *  DarkReader's part, which returns early when the library is missing. In
 *  a darkened page the bootstrap runs DarkReader with the skin's colours.
 *  `window.fetch` is BOUND: DarkReader calls the stored function detached,
 *  and an unbound fetch throws "Illegal invocation". DarkReader's part is
 *  guarded (`window.DarkReader`, try/catch), so a library that did not load
 *  leaves the page as authored; it disables before it re-enables (the
 *  pinned build keeps its first colours on a bare re-enable). `<` is
 *  escaped in the options and the block, so nothing can close the script
 *  or style element. Every tag carries the nonce when one is given. Empty
 *  when there is neither DarkReader, nor a block, nor the report, nor the
 *  picker. */
function viewBlock(
    dark: boolean, options: DarkReaderOptions, variables: string | null, injection: DarkReaderInjection,
    pointer = false, pick = false,
): string {
    const darken = dark && !!injection && !!injection.src;
    if (!darken && variables === null && !pointer && !pick) return '';
    const nonce = injection.nonce && NONCE.test(injection.nonce) ? ` nonce="${injection.nonce}"` : '';
    const lib = darken ? `<script src="${injection.src.replace(/"/g, '%22')}"${nonce}></script>` : '';
    const sheet = variables === null ? ''
        : `<style id="${DOCUMENT_VARIABLES_ID}">${variables.replace(/</g, '\\3c ')}</style>`;
    // The latest position, posted once per animation frame: passive and in
    // the capture phase, so the page's own handlers neither see a
    // difference nor can stop it.
    const report = !pointer ? ''
        : 'try{var q=null,t=0,s=function(){t=0;window.parent.postMessage({type:"' + POINTER_MESSAGE
            + '",x:q[0],y:q[1],w:window.innerWidth,h:window.innerHeight},"*");};'
            + 'window.addEventListener("pointermove",function(e){q=[e.clientX,e.clientY];'
            + 'if(!t)t=window.requestAnimationFrame(s);},{capture:true,passive:true});}catch(x){}';
    // Ahead of DarkReader's part, which returns early without the library.
    const picker = pick ? 'try{' + PICK_BRIDGE_JS + '}catch(x){}' : '';
    const listen = variables === null ? ''
        : `var v=document.getElementById("${DOCUMENT_VARIABLES_ID}");`
            + 'window.addEventListener("message",function(e){' + FROM_HOST
            + 'if(v&&typeof d.variables==="string")v.textContent=d.variables;});';
    let recolour = '';
    if (darken) {
        const json = JSON.stringify(options || {}).replace(/</g, '\\u003c');
        recolour = 'if(!window.DarkReader)return;'
            + 'DarkReader.setFetchMethod(window.fetch.bind(window));'
            + `DarkReader.enable(${json});`
            + 'window.addEventListener("message",function(e){' + FROM_HOST
            + 'try{DarkReader.disable();DarkReader.enable(d.options||{});}catch(x){}'
            + '});';
    }
    return lib + sheet + `<script${nonce}>(function(){try{` + listen + report + picker + recolour
        + '}catch(e){}})();</script>';
}

// The meta is void; a serializer that writes `</meta>` anyway is tolerated.
const LEADING_CSP = /^\s*<meta\b[^>]*\bhttp-equiv\s*=\s*["']?content-security-policy["']?[^>]*>(?:\s*<\/meta>)?/i;

/** Splice `block` into the FRONT of the document's head. `afterCsp` keeps a
 *  Content-Security-Policy meta that leads the head first, so the policy
 *  governs the block too. `\b` after `head`/`html` keeps `<header>` and
 *  `<htmlish>` from matching; a structureless fragment gets the block first,
 *  never ahead of a doctype (the page would drop into quirks mode). */
function atHeadFront(src: string, block: string, afterCsp: boolean): string {
    const at = (i: number) => src.slice(0, i) + block + src.slice(i);
    const head = /<head\b[^>]*>/i.exec(src);
    if (head) {
        let i = head.index + head[0].length;
        const csp = afterCsp ? LEADING_CSP.exec(src.slice(i)) : null;
        if (csp) i += csp[0].length;
        return at(i);
    }
    const htmlTag = /<html\b[^>]*>/i.exec(src);
    if (htmlTag) return at(htmlTag.index + htmlTag[0].length);
    const doctype = /^\s*<!doctype[^>]*>/i.exec(src);
    return at(doctype ? doctype[0].length : 0);
}

/** Splice DarkReader into the FRONT of the document's head. `on === false`
 *  (or no library URL) returns the input untouched. With a nonce, a
 *  Content-Security-Policy meta that leads the head stays first, so the
 *  policy governs the injected tags too. */
export function injectDarkReader(
    html: string, on: boolean, options: DarkReaderOptions, injection: DarkReaderInjection,
): string {
    const src = html || '';
    if (!on || !injection || !injection.src) return src;
    return atHeadFront(src, viewBlock(true, options, null, injection), !!injection.nonce);
}

/** What a renderer's prepare step hands the view: the document to show and
 *  the nonce its own CSP admits scripts by, if it has one. */
export interface PreparedDocument {
    html: string;
    nonce?: string;
}

/** The srcDoc a view shows: the prepared document, darkened or not, with
 *  the variables block (`null` for a view without one), the pointer report
 *  (`pointer`) and the element picker (`pick`). The one place a view's
 *  chrome is added, so the same file gets the same injection in any
 *  renderer. */
export function documentSrcDoc(
    prepared: PreparedDocument, dark: boolean, options: DarkReaderOptions, src: string,
    variables: string | null = null, pointer = false, pick = false,
): string {
    const html = prepared.html || '';
    const block = viewBlock(dark, options, variables, { src, nonce: prepared.nonce }, pointer, pick);
    return block ? atHeadFront(html, block, !!prepared.nonce) : html;
}

// ── the host's theme, and knowing it is the HOST's ────────────────────────

/** Is this snapshot one the host really pushed, or the placeholder an iframe
 *  renderer reads before the host has said anything (`known: false`, scheme
 *  `'dark'` whatever the skin)? The scheme decides what is IN the document,
 *  so the view waits for a real one rather than load the page twice. */
export function hostThemeKnown(theme: unknown): boolean {
    return !!theme && typeof theme === 'object' && (theme as { known?: unknown }).known === true;
}

/** How long to wait for the host's first theme before rendering on what the
 *  snapshot says: a host with no theme wiring must not leave a blank pane. */
export const HOST_THEME_WAIT_MS = 1500;

/** Whether the theme in hand is the host's answer: a pushed snapshot, or a
 *  host that cannot push one (no `onThemeChange`). */
export function themeSettled(theme: unknown, api: unknown): boolean {
    const push = api && typeof api === 'object' ? (api as { onThemeChange?: unknown }).onThemeChange : undefined;
    return hostThemeKnown(theme) || typeof push !== 'function';
}

/** What a page takes from the skin, as the key the view compares: DarkReader's
 *  colours (a darkened page's only) and the variables block (`null` for a
 *  view without one). */
export function documentSkinKey(dark: boolean, options: DarkReaderOptions, variables: string | null): string {
    return JSON.stringify({ options: dark ? options : {}, variables });
}

/** The message to post into the loaded page, or null: only a page with a
 *  listener (`listening`: darkened, or carrying a variables block) is told,
 *  and only a skin it does not already have is worth a post. */
export function documentSkinPost(
    listening: boolean, delivered: string, skinKey: string,
): { type: typeof DARKREADER_MESSAGE; options: DarkReaderOptions; variables: string | null } | null {
    if (!listening || delivered === skinKey) return null;
    const { options, variables } = JSON.parse(skinKey);
    return { type: DARKREADER_MESSAGE, options, variables };
}

/** What a page has once it loads: the skin baked into its srcDoc. A post
 *  made while a swapped srcDoc was loading may have reached the outgoing
 *  document, so a load resets to the baked skin. */
export function deliveredAfterLoad(previewKey: string | null | undefined): string {
    return previewKey ?? '';
}

// ── the backing and the view's styles ─────────────────────────────────────

/** The backing behind the page: the ground the page will paint, so a srcDoc
 *  swap shows no seam.
 *
 *  The backing's colour scheme must be the page's. When a frame's
 *  `color-scheme` differs from its embedder's, the browser gives the frame
 *  an opaque Canvas of the frame's own scheme, over the backing, wherever
 *  the page paints no ground: a light page on a dark backing shows white,
 *  a dark page on a light backing near-black. Hence:
 *
 *  - Before the scheme is settled, transparent: either ground on a guess is
 *    a flash the other way. No frame is mounted then: the view's srcDoc is
 *    null until the page is built, and a renderer mounts its iframe only
 *    once it is not. An empty frame is a light page, and on the app's dark
 *    scheme the browser would paint it white.
 *  - Darkened: the skin token DarkReader was given as the ground. Inside an
 *    iframe DarkReader does not paint the root's ground, so a page without
 *    one shows this through; `color-scheme: dark` matches the darkened
 *    document's.
 *  - Shown as authored in a light skin: `--sheet`, one shade off a page's
 *    white.
 *  - Shown as authored in a dark skin: the page's own default ground, the
 *    browser's `Canvas` under a light scheme. A system colour keyword, the
 *    page's colour and never chrome: a dark sheet would flash on every swap
 *    under a white page.
 */
export function frameBacking(settled: boolean, dark: boolean, scheme: Scheme): React.CSSProperties {
    if (!settled) return { background: 'transparent' };
    if (dark) return { background: `var(${darkReaderPoles(scheme).ground})`, colorScheme: 'dark' };
    if (scheme === 'light') return { background: 'var(--sheet)' };
    return { background: 'Canvas', colorScheme: 'light' };
}

/** The view's styles. No frame: the page wrapper is the renderer's whole
 *  box, painted with the backing. `page` fills its box; `iframe` fills the
 *  wrapper. The dark choice is the renderer's `StyleControl`, which covers
 *  nothing the page needs: it shows only while the pointer is near its
 *  corner (the page reports the pointer, `reportPointer`). */
export const DOCUMENT_VIEW_STYLE = {
    page: {
        position: 'relative', boxSizing: 'border-box', width: '100%', height: '100%',
    } as React.CSSProperties,
    iframe: {
        display: 'block', width: '100%', height: '100%', border: 'none',
    } as React.CSSProperties,
};

// ── the view ──────────────────────────────────────────────────────────────

export interface DocumentViewInput {
    /** The document as stored, or null while there is none (no srcDoc). */
    html: string | null;
    /** The renderer api (`onThemeChange` tells a host that will push). */
    api: unknown;
    /** The skin record, `useTheme(api)`. */
    theme: ThemeLike;
    /** Whether to darken the page: true darkens it, false shows it as
     *  authored, null follows the skin's scheme (darkened in a dark skin).
     *  The renderer takes it from its style (`'darkened'`, `'authored'`,
     *  null); a change rebuilds the srcDoc. */
    dark: boolean | null;
    /** The renderer's own prepare step. Keep it stable (useCallback): a new
     *  function rebuilds the srcDoc, which reloads the page. It takes nothing
     *  from the skin: that is `variables`. */
    prepare: (html: string) => PreparedDocument;
    /** The page's variables block, CSS text (a `:root` rule). The view
     *  writes it into the first srcDoc and replaces it in the loaded page on
     *  every change, without a reload. Omitted for a page that takes
     *  nothing from the renderer. */
    variables?: string;
    /** The page reports the pointer to the parent window (`POINTER_MESSAGE`)
     *  for a `StyleControl` floating over it (pass it the view's `frameRef`
     *  as `frame`). Off by default: a page with nothing over it has nothing
     *  to tell. */
    reportPointer?: boolean;
    /** Add the element picker to the page's bootstrap; the view's `pick`
     *  then lets the user click an element. Off by default. */
    pickElements?: boolean;
}

export interface DocumentView {
    /** The srcDoc to show, or null while there is none to build yet. */
    srcDoc: string | null;
    frameRef: React.MutableRefObject<HTMLIFrameElement | null>;
    onLoad: () => void;
    /** Whether the page is darkened: the caller's choice, else the skin's
     *  scheme. */
    dark: boolean;
    /** The backing alone (`frameBacking`), for a page wrapper the renderer
     *  lays out itself. */
    backing: React.CSSProperties;
    /** The page wrapper's style for a renderer whose root is the view: it
     *  fills the box (`DOCUMENT_VIEW_STYLE.page`), on the backing. */
    pageStyle: React.CSSProperties;
    /** Whether the loaded page's picker has answered (`pickElements`). */
    pickAvailable: boolean;
    /** Let the user click an element of the page: resolves the element,
     *  null when the user presses Esc in the page, `signal` aborts or a
     *  newer pick replaces this one; rejects when the view was built
     *  without `pickElements`, or when the page's picker does not answer
     *  within `PICK_READY_WAIT_MS` of a load. `hint` (at most 200
     *  characters) is shown over the page. A reload re-arms a pending pick. */
    pick: (signal: AbortSignal, hint?: string) => Promise<ElementPick | null>;
}

interface PendingPick {
    id: string;
    hint: string;
    signal: AbortSignal;
    onAbort: () => void;
    timer: number;
    resolve: (value: ElementPick | null) => void;
    reject: (error: Error) => void;
}

/** Settle `p` once, if it is still the pending pick: clear its wait and its
 *  abort listener, then reject with an Error or resolve with the value. */
function settlePick(slot: { current: PendingPick | null }, p: PendingPick, value: ElementPick | null | Error): void {
    if (slot.current !== p) return;
    slot.current = null;
    if (p.timer) window.clearTimeout(p.timer);
    p.signal.removeEventListener('abort', p.onAbort);
    if (value instanceof Error) p.reject(value); else p.resolve(value);
}

function pickId(): string {
    const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
    if (c && typeof c.randomUUID === 'function') return c.randomUUID();
    return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

/** The shared view: the gate on the host's theme, the dark decision, the
 *  srcDoc (prepare, then DarkReader, the variables block, the pointer
 *  report and the picker), the live recolour, the backing and the pick.
 *  The renderer draws `<div style={pageStyle}>{srcDoc !== null && <iframe
 *  ref={frameRef} srcDoc={srcDoc} onLoad={onLoad}
 *  style={DOCUMENT_VIEW_STYLE.iframe} sandbox="…"/>}</div>` (no empty frame
 *  before the page is built, see `frameBacking`) and offers the dark choice
 *  in its `StyleControl`. */
export function useDocumentView({
    html, api, theme, dark: choice, prepare, variables, reportPointer = false, pickElements = false,
}: DocumentViewInput): DocumentView {
    const scheme: Scheme = theme && theme.scheme === 'light' ? 'light' : 'dark';
    const pushed = themeSettled(theme, api);
    const [waited, setWaited] = useState(false);
    useEffect(() => {
        if (pushed || waited) return undefined;
        const id = window.setTimeout(() => setWaited(true), HOST_THEME_WAIT_MS);
        return () => window.clearTimeout(id);
    }, [pushed, waited]);
    const dark = typeof choice === 'boolean' ? choice : scheme === 'dark';
    // Every page waits for the host: the skin's colours arrive with its theme.
    const settled = pushed || waited;
    const src = useMemo(darkReaderScriptUrl, []);
    // The srcDoc bakes the skin current when it is built (DarkReader's
    // colours and the variables block); a skin change alone is posted into
    // the page (deliver) instead of rebuilding, which would reload the
    // user's page.
    const options = useMemo(() => darkReaderOptions(theme), [theme && theme.vars, scheme]);
    const colors = useMemo(() => pickColors(theme), [theme && theme.vars]);
    const block = typeof variables === 'string' ? variables : null;
    const hasBlock = block !== null;
    const skinKey = documentSkinKey(dark, options, block);
    const latest = useRef({ options, block, colors });
    latest.current = { options, block, colors };
    const preview = useMemo(() => {
        if (!settled || html === null) return null;
        const baked = latest.current;
        return {
            html: documentSrcDoc(prepare(html), dark, baked.options, src, baked.block, reportPointer, pickElements),
            key: documentSkinKey(dark, baked.options, baked.block),
        };
    }, [html, prepare, dark, settled, src, hasBlock, reportPointer, pickElements]);
    const frameRef = useRef<HTMLIFrameElement | null>(null);
    const delivered = useRef('');
    const listening = dark || hasBlock;
    const deliver = useCallback(() => {
        const win = frameRef.current ? frameRef.current.contentWindow : null;
        const message = win ? documentSkinPost(listening, delivered.current, skinKey) : null;
        if (!win || !message) return;
        win.postMessage(message, '*');
        delivered.current = skinKey;
    }, [listening, skinKey]);
    // The picker: whether the loaded page's bridge has answered, and the
    // one pick in flight.
    const pickReady = useRef(false);
    const pending = useRef<PendingPick | null>(null);
    const [pickAvailable, setPickAvailable] = useState(false);
    const postToPage = useCallback((message: unknown) => {
        const win = frameRef.current ? frameRef.current.contentWindow : null;
        if (win) win.postMessage(message, '*');
    }, []);
    // A pending pick waits a bounded time for the page's bridge to answer.
    const awaitBridge = useCallback(() => {
        const p = pending.current;
        if (!p) return;
        if (p.timer) window.clearTimeout(p.timer);
        p.timer = window.setTimeout(() => {
            if (pending.current === p && !pickReady.current) settlePick(pending, p, new Error(PICK_UNREACHABLE));
        }, PICK_READY_WAIT_MS);
    }, []);
    useEffect(() => {
        delivered.current = deliveredAfterLoad(preview && preview.key);
        pickReady.current = false;
        setPickAvailable(false);
    }, [preview]);
    useEffect(deliver, [deliver]);
    const onLoad = useCallback(() => {
        delivered.current = deliveredAfterLoad(preview && preview.key);
        deliver();
        if (pickElements) { pickReady.current = false; setPickAvailable(false); postToPage({ type: PICK_MESSAGE.probe }); if (pending.current) awaitBridge(); }
    }, [preview, deliver, pickElements, postToPage, awaitBridge]);
    useEffect(() => {
        if (!pickElements) return undefined;
        const onMessage = (event: MessageEvent) => {
            const frame = frameRef.current;
            if (!frame || event.source !== frame.contentWindow) return;
            const data = event.data as { type?: unknown; id?: unknown } | null;
            if (!data || typeof data.type !== 'string') return;
            const p = pending.current;
            if (data.type === PICK_MESSAGE.ready) {
                pickReady.current = true;
                setPickAvailable(true);
                if (p) {
                    if (p.timer) { window.clearTimeout(p.timer); p.timer = 0; }
                    postToPage({ type: PICK_MESSAGE.start, id: p.id, hint: p.hint, colors: latest.current.colors });
                }
                return;
            }
            if (!p || data.id !== p.id) return;
            if (data.type === PICK_MESSAGE.picked) settlePick(pending, p, elementPickOf(data));
            else if (data.type === PICK_MESSAGE.abandoned) settlePick(pending, p, null);
        };
        window.addEventListener('message', onMessage);
        return () => {
            window.removeEventListener('message', onMessage);
            if (pending.current) settlePick(pending, pending.current, null);
        };
    }, [pickElements, postToPage]);
    const pick = useCallback((signal: AbortSignal, hint?: string) => new Promise<ElementPick | null>((resolve, reject) => {
        if (!pickElements) { reject(new Error('This view was built without pickElements.')); return; }
        if (signal.aborted) { resolve(null); return; }
        const prior = pending.current;
        if (prior) {
            postToPage({ type: PICK_MESSAGE.cancel, id: prior.id });
            settlePick(pending, prior, null);
        }
        const p: PendingPick = {
            id: pickId(), hint: (hint ?? '').slice(0, 200), signal, timer: 0, resolve, reject,
            onAbort: () => {
                postToPage({ type: PICK_MESSAGE.cancel, id: p.id });
                settlePick(pending, p, null);
            },
        };
        pending.current = p;
        signal.addEventListener('abort', p.onAbort, { once: true });
        if (pickReady.current) postToPage({ type: PICK_MESSAGE.start, id: p.id, hint: p.hint, colors: latest.current.colors });
        else awaitBridge();
    }), [pickElements, postToPage, awaitBridge]);
    const backing = frameBacking(settled, dark, scheme);
    return {
        srcDoc: preview ? preview.html : null,
        frameRef,
        onLoad,
        dark,
        backing,
        pageStyle: { ...DOCUMENT_VIEW_STYLE.page, ...backing },
        pickAvailable,
        pick,
    };
}

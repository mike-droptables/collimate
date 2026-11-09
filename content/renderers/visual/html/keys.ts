// SPDX-License-Identifier: MIT
// ---------------------------------------------------------------------------
// The page's key bridge: the host's shortcuts keep working while the keys
// are in the page.
//
// The page runs in an opaque srcdoc frame of its own, a browsing context
// the host's frame (sandbox.html) never sees a key of. The prepare step puts
// a few lines of script into its head (`keyBridge`), beside the link bridge,
// which speak sandbox.html's page key protocol (its §3d):
//   - on start it asks its parent for the host's chords (`hello`);
//   - the parent answers with them (`keys`: the same combos sandbox.html
//     forwards, each with a modifier), and again on every remap;
//   - a keydown matching one is consumed in the page's capture phase
//     (preventDefault and stopImmediatePropagation: the page's own handlers
//     and the browser's default never act on it) and reported (`keydown`);
//   - every modifier release is reported (`keyup`), so a cell move started
//     from the page commits;
//   - a bare Esc is the page's, but a second within RELEASE_DOUBLE_TAP_MS
//     is consumed and reported (`release`): the release gesture sandbox.html
//     counts on its own window (its §3b), which it then forwards the same
//     way, so the host's back cascade moves the keyboard to the cell's card.
// Any other bare key (typing, the arrows) is never a host chord: it stays
// the page's. While the document view's element picker is armed its capture
// listener, which the view puts ahead of this bridge, swallows every key
// first, so the bridge stays quiet during a pick.
//
// Its limits: a page whose own Content-Security-Policy refuses inline script
// runs without it, and a page that navigates away leaves it behind.
// ---------------------------------------------------------------------------

/** The page key protocol's messages (sandbox.html §3d). */
export const PAGE_KEY_MESSAGES = Object.freeze({
    hello: 'collimate:page-keys-hello',
    keys: 'collimate:page-keys',
    keydown: 'collimate:page-keydown',
    keyup: 'collimate:page-keyup',
    release: 'collimate:page-release',
});

/** Two bare Escs this close (ms) release the page to the host: the gesture
 *  sandbox.html §3b counts, and the host for a native renderer. */
export const RELEASE_DOUBLE_TAP_MS = 600;

/** The key bridge's script body (see the header). It names only `window`
 *  and built-ins and holds no `<`. */
export function keyBridge(): string {
    const m = JSON.stringify(PAGE_KEY_MESSAGES);
    return '(function(){var M=' + m + ',P=window.parent,K=[],E=0;'
        + 'function post(d){try{P.postMessage(d,"*");}catch(x){}}'
        + 'function report(t,e){post({type:t,key:e.key,code:e.code,altKey:e.altKey,ctrlKey:e.ctrlKey,shiftKey:e.shiftKey,metaKey:e.metaKey});}'
        + 'function low(k){return k&&k.length===1?k.toLowerCase():k;}'
        + 'function same(e,c){if(c.code?e.code!==c.code:low(e.key)!==low(c.key))return false;'
        + 'return !!e.metaKey===!!c.metaKey&&!!e.ctrlKey===!!c.ctrlKey&&!!e.altKey===!!c.altKey&&!!e.shiftKey===!!c.shiftKey;}'
        + 'window.addEventListener("message",function(e){if(e.source!==P)return;var d=e.data;'
        + 'if(d&&d.type===M.keys&&Array.isArray(d.combos))K=d.combos;});'
        + 'window.addEventListener("keydown",function(e){'
        + 'if(e.key==="Escape"&&!e.altKey&&!e.ctrlKey&&!e.shiftKey&&!e.metaKey){var n=Date.now();'
        + 'if(n-E>=' + RELEASE_DOUBLE_TAP_MS + '){E=n;return;}'
        + 'E=0;e.preventDefault();e.stopImmediatePropagation();post({type:M.release});return;}'
        + 'if(!K.some(function(c){return same(e,c);}))return;'
        + 'e.preventDefault();e.stopImmediatePropagation();report(M.keydown,e);},true);'
        + 'window.addEventListener("keyup",function(e){'
        + 'if(e.key==="Alt"||e.key==="Control"||e.key==="Meta"||e.key==="Shift")report(M.keyup,e);},true);'
        + 'post({type:M.hello});})();';
}

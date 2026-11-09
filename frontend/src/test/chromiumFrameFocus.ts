/**
 * Chromium's iframe.blur(): a focused frame hands the keys back to the top
 * document. jsdom 30 follows the spec instead, where a focused frame's focus
 * target is its content document, so blur() on the frame element leaves it
 * focused. The app runs only on Chromium (the browser and Electron), and the
 * focus hand-offs rely on its behaviour (lib/blurForeignEditor,
 * lib/selectionFocus), so jsdom tests install it: src/test/setup.ts imports
 * this module, and a node-project test in a jsdom environment imports it
 * itself.
 */
const elementBlur = HTMLElement.prototype.blur;
HTMLIFrameElement.prototype.blur = function blurFrame(this: HTMLIFrameElement) {
    if (this.ownerDocument.activeElement === this) this.ownerDocument.documentElement.focus();
    else elementBlur.call(this);
};

export {};

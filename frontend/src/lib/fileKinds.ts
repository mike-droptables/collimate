// File-kind extension lists shared by the renderer dispatch (useRendererId),
// the compare flow and the zoom-transition preview. Kept in a pure module so
// importing it pulls in no renderer registry side effects.

export const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.ico', '.avif', '.tiff', '.tif'];
/** Binary files with a renderer of their own (the registry's `pdf`), which
 *  reads the bytes through the file URL; never text-fed. */
export const PDF_EXTS = ['.pdf'];
/** Binary files no renderer shows: the built-in binary view. */
export const BINARY_EXTS = ['.zip', '.tar', '.gz', '.mp4', '.webm', '.wasm'];

export function isImagePath(name: string): boolean {
    const lower = name.toLowerCase();
    return IMAGE_EXTS.some(ext => lower.endsWith(ext));
}

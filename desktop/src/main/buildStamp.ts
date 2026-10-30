import { readFileSync } from 'node:fs';
import path from 'node:path';

/**
 * What the release's packaging config stamped into the packaged
 * package.json (electron-builder's `extraMetadata`, written by
 * build/desktop_packaging.py): `collimate.signed`, whether the build
 * carries a code signature. The updater reads it (an unsigned macOS app
 * cannot update itself). A package.json without the stamp — a dev run from
 * desktop/, a local build/build_desktop.sh package — is an unsigned build.
 */
export function buildSigned(appPath: string): boolean {
  let text: string;
  try {
    text = readFileSync(path.join(appPath, 'package.json'), 'utf8');
  } catch {
    return false;
  }
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== 'object' || parsed === null) return false;
    const stamp = (parsed as { collimate?: unknown }).collimate;
    if (typeof stamp !== 'object' || stamp === null) return false;
    return (stamp as { signed?: unknown }).signed === true;
  } catch {
    return false;
  }
}

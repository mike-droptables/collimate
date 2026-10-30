import type { GuestDevice } from '../shared/bridge.js';

/**
 * How a guest presents as each device: the one place user agent strings
 * are composed and the screen metrics are decided.
 *
 * The page names a device (`GuestCreateOptions.device`,
 * `setGuestDevice`); the shell owns what that means, so the user agent
 * always names the Chromium the guest really runs (`process.versions.chrome`)
 * and a page can never smuggle in an arbitrary one. The widths match the
 * web generator's `DEVICES` table (`_web_shared/protocol.py`), which the
 * webview renderer letterboxes the guest to.
 */

export const GUEST_DEVICES: readonly GuestDevice[] = ['desktop', 'phone', 'tablet'];

export function isGuestDevice(value: unknown): value is GuestDevice {
  return typeof value === 'string' && (GUEST_DEVICES as readonly string[]).includes(value);
}

/** `Emulation.setDeviceMetricsOverride`'s parameters, sent through the
 *  guest's CDP session (`webContents.debugger`). Electron 45's
 *  `enableDeviceEmulation` no longer applies a page's `width=device-width`
 *  viewport once the view is letterboxed (the page lays out at the 980 px
 *  fallback); the CDP override with `mobile` does. */
export interface DeviceMetrics {
  /** Zero: the view keeps its own bounds (the renderer letterboxes them at
   *  the device width), so the page never lays out wider than it shows. */
  width: 0;
  height: 0;
  /** Zero: the window's own scale factor. */
  deviceScaleFactor: 0;
  /** A mobile screen: the page's viewport meta applies. */
  mobile: true;
  screenWidth: number;
  screenHeight: number;
}

/** What a guest presenting as a device gets: its user agent and its
 *  screen metrics (null = none, the plain desktop view). */
export interface DevicePresentation {
  userAgent: string;
  metrics: DeviceMetrics | null;
}

const SCREENS: Record<Exclude<GuestDevice, 'desktop'>, { width: number; height: number }> = {
  phone: { width: 412, height: 915 },
  tablet: { width: 800, height: 1280 },
};

/** The screen metrics of `device`; null for desktop (none). */
export function deviceMetrics(device: GuestDevice): DeviceMetrics | null {
  if (device === 'desktop') return null;
  return {
    width: 0,
    height: 0,
    deviceScaleFactor: 0,
    mobile: true,
    screenWidth: SCREENS[device].width,
    screenHeight: SCREENS[device].height,
  };
}

/** Chrome's reduced user agent carries the major version only. */
function chromeMajor(chromeVersion: string): string {
  return /^\d+/.exec(chromeVersion)?.[0] ?? '0';
}

/**
 * The presentation of `device`. Desktop is the shell's own user agent and
 * no metrics override; phone is Android Chrome Mobile; tablet is Chrome on an
 * Android tablet (the same string without `Mobile`, which is how sites
 * tell the two apart).
 */
export function devicePresentation(
  device: GuestDevice,
  shellUserAgent: string,
  chromeVersion: string = process.versions.chrome ?? '',
): DevicePresentation {
  if (device === 'desktop') return { userAgent: shellUserAgent, metrics: null };
  const chrome = `Chrome/${chromeMajor(chromeVersion)}.0.0.0`;
  const userAgent = device === 'phone'
    ? `Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) ${chrome} Mobile Safari/537.36`
    : `Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) ${chrome} Safari/537.36`;
  return { userAgent, metrics: deviceMetrics(device) };
}

import { expect, it, vi } from 'vitest';
import { userEvent } from 'vitest/browser';
import { mirrorUrl } from '@/lib/moduleMirror';

it('actual scoped terminal fits, paints, accepts input and repaints its cached rows __F1710', async () => {
  const source = '/api/renderers/fixture-step6-terminal/source';
  const module = await import(/* @vite-ignore */ source);
  class Transport {
    static OPEN = 1;
    static all: Transport[] = [];
    readyState = 0;
    binaryType = '';
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    onclose: ((event: { code: number }) => void) | null = null;
    onerror = null;
    sent: (string | Uint8Array)[] = [];
    constructor(_url: string) { Transport.all.push(this); }
    send(value: string | Uint8Array) { this.sent.push(value); }
    close() { this.readyState = 3; }
    open() { this.readyState = 1; this.onopen?.(); }
    output(data: string) { this.onmessage?.({ data }); }
    end() { this.readyState = 3; this.onclose?.({ code: 1000 }); }
  }
  const fontBefore = localStorage.getItem('collimate.terminal.fontSize');
  localStorage.setItem('collimate.terminal.fontSize', '13');
  vi.stubGlobal('WebSocket', Transport);
  const shell = document.createElement('div');
  shell.style.cssText = 'width:480px;height:240px;position:relative';
  const shadow = shell.attachShadow({ mode: 'open' });
  const host = document.createElement('div');
  host.style.cssText = 'width:100%;height:100%';
  shadow.append(host); document.body.append(shell);
  const styles: Promise<void>[] = [];
  const visibilityListeners = new Set<(value: boolean) => void>();
  const pointer = JSON.stringify({ terminal_id: crypto.randomUUID(), ws_path: '/__controlled_terminal', label: 'Dependency terminal' });
  const api = {
    getContent: async () => pointer, getFilename: () => 'fixture.terminal', isReadOnly: () => false,
    getSurface: async () => null, onSurfaceChange: () => () => {},
    // As the hosts do: a module-CDN stylesheet comes from the mirror.
    loadCss: (url: string) => {
      styles.push(fetch(mirrorUrl(url, '/api')).then(async response => {
        expect(response.ok).toBe(true);
        const style = document.createElement('style'); style.textContent = await response.text(); shadow.append(style);
      }));
    },
    getVisible: () => true,
    onVisibleChange: (callback: (value: boolean) => void) => {
      visibilityListeners.add(callback); return () => { visibilityListeners.delete(callback); };
    },
    reportChange: () => {}, onFilesChange: () => () => {}, onSetContent: () => () => {}, onFlush: () => () => {}, ready: () => {},
  };
  let cleanup: (() => void) | undefined;
  const resizeFrames = () => Transport.all[0]!.sent.filter((value): value is string => typeof value === 'string')
    .map(value => JSON.parse(value) as { t: string; cols: number; rows: number }).filter(frame => frame.t === 'resize');
  // xterm 6 draws with its DOM renderer: each row is a div of styled spans.
  const rows = () => [...host.querySelectorAll<HTMLElement>('.xterm-rows > div')];
  const painted = () => rows().some(row => [...row.querySelectorAll('span')].some(span => {
    if (!(span.textContent || '').includes('SCOPED')) return false;
    // ANSI red output (the palette's red, red-dominant), rather than the
    // cursor or a blank row, proves repaint.
    const [r, g, b] = (getComputedStyle(span).color.match(/\d+/g) ?? []).map(Number);
    return r! > g! + 50 && r! > b! + 50;
  }));
  try {
    cleanup = await module.default.mount(host, api);
    await vi.waitFor(() => expect(Transport.all).toHaveLength(1));
    await Promise.all(styles);
    const socket = Transport.all[0]!; socket.open();
    await vi.waitFor(() => expect(resizeFrames().at(-1)?.cols).toBeGreaterThan(20));
    await vi.waitFor(() => expect(rows().length).toBeGreaterThan(0), { timeout: 5000 });
    socket.output('\x1b[2J\x1b[H\x1b[31mSCOPED TERMINAL OUTPUT\x1b[0m');
    await vi.waitFor(() => expect(painted()).toBe(true));
    const first = resizeFrames().at(-1)!;
    shell.style.width = '640px';
    await vi.waitFor(() => expect(resizeFrames().at(-1)!.cols).toBeGreaterThan(first.cols));
    const wide = resizeFrames().at(-1)!;
    host.querySelector<HTMLButtonElement>('button[title="Increase font size"]')!.click();
    await vi.waitFor(() => expect(resizeFrames().at(-1)!.cols).toBeLessThan(wide.cols));
    const input = host.querySelector<HTMLTextAreaElement>('textarea')!;
    await userEvent.type(input, 'typed');
    expect(socket.sent.filter(value => value instanceof Uint8Array).map(value => new TextDecoder().decode(value)).join('')).toContain('typed');
    // No new output: visibility recovery and cache remount must repaint the existing buffer.
    for (const row of rows()) row.replaceChildren();
    expect(painted()).toBe(false);
    visibilityListeners.forEach(listener => listener(true));
    await vi.waitFor(() => expect(painted()).toBe(true));
    cleanup!(); cleanup = await module.default.mount(host, api);
    await vi.waitFor(() => expect(painted()).toBe(true));
    expect(Transport.all).toHaveLength(1);
    socket.end();
  } finally {
    cleanup?.(); shell.remove(); vi.unstubAllGlobals();
    if (fontBefore === null) localStorage.removeItem('collimate.terminal.fontSize');
    else localStorage.setItem('collimate.terminal.fontSize', fontBefore);
    delete (window as unknown as { __step6TerminalLink?: unknown }).__step6TerminalLink;
  }
}, 30000);

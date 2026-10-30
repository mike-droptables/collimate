import {setRendererHandlers} from '@/lib/rendererAliases';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, act, waitFor } from '@testing-library/react';
import { PooledIframeConcealContext, RendererConcealContext } from '@/contexts/RendererConcealContext';

// The pool as SandboxIframe sees it. `acquire` models un-parking an entry
// that is already ready: the real pool reveals such an entry on the spot.
// Re-acquiring with a different src models the pool's physical document replacement,
// which hides the element until the renderer is ready again.
const revealed = new Map<string, boolean>();
const srcs = new Map<string, string>();
const handlers = new Map<string, (e: MessageEvent) => void>();
const currentWindow = {postMessage: vi.fn()} as unknown as Window;
const unregisterMotionLayer = vi.fn();
const pool = {
    acquire: vi.fn((id: string, src: string, _container?: HTMLElement) => {
        const prev = srcs.get(id);
        srcs.set(id, src);
        revealed.set(id, prev === undefined || prev === src);
    }),
    release: vi.fn(),
    drop: vi.fn(),
    setPointerEvents: vi.fn(),
    getContentWindow: vi.fn<() => Window | null>(() => currentWindow),
    getElement: vi.fn<(id: string) => HTMLElement | null>(() => null),
    waitUntilReady: vi.fn(() => new Promise<void>(() => {})),
    setVisible: vi.fn((id: string, visible: boolean) => { revealed.set(id, visible); }),
    onMessage: vi.fn((id: string, h: (e: MessageEvent) => void) => { handlers.set(id, h); return () => {}; }),
    registerMotionLayer: vi.fn((_el: HTMLElement) => unregisterMotionLayer),
};
vi.mock('@/contexts/GuestPoolContext', () => ({ useGuestPool: () => pool }));
// AnimatePresence's verdict for the host cell: false = exit-animating.
const presence = vi.hoisted(() => ({ present: true }));
vi.mock('motion/react', async (original) => ({
    ...(await original<typeof import('motion/react')>()),
    useIsPresent: () => presence.present,
}));
const { saveSiblingFileInCell } = vi.hoisted(() => ({
    saveSiblingFileInCell: vi.fn(async (..._args: unknown[]) => ({ ok: true, reason: undefined as string | undefined })),
}));
vi.mock('@/lib/siblingWrite', () => ({ saveSiblingFileInCell }));

import { SandboxIframe } from './SandboxIframe';
import { StandaloneSandboxIframe } from './StandaloneSandboxIframe';
import { RETIRED_TOKENS } from '@/test/retiredTokens';
import { applyEditingSnapshot } from '@/lib/editingSnapshot';
import { annotationResponders, requestAnnotation } from '@/lib/rendererAnnotation';

class ImmediateObserver {
    constructor(private cb: IntersectionObserverCallback) {}
    observe(el: Element) {
        this.cb([{ isIntersecting: true, target: el } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
    }
    unobserve() {}
    disconnect() {}
}

beforeEach(() => {
    vi.clearAllMocks();
    pool.getContentWindow.mockReturnValue(currentWindow);
    pool.getElement.mockImplementation(() => null);
    vi.mocked(currentWindow.postMessage).mockImplementation((message)=>{if(message.type==='FLUSH_DRAFT')queueMicrotask(()=>window.dispatchEvent(new MessageEvent('message',{origin:window.location.origin,source:currentWindow,data:{type:'FLUSH_DRAFT_ACK',requestId:message.requestId}})));});
    revealed.clear();
    srcs.clear();
    handlers.clear();
    presence.present = true;
    vi.stubGlobal('IntersectionObserver', ImmediateObserver);
});
afterEach(() => { cleanup(); setRendererHandlers([]); vi.unstubAllGlobals(); });

const props = { documentId: 'document:a', fileContent: 'current owner body', fileHash: 'h', fileName: 'a.md', rendererId: 'r', isActiveFocus: false };

describe('SandboxIframe conceal', () => {
    it('keeps a concealed renderer hidden when acquiring un-parks an already-ready entry', () => {
        render(
            <RendererConcealContext.Provider value={true}>
                <SandboxIframe {...props} />
            </RendererConcealContext.Provider>,
        );
        expect(pool.acquire).toHaveBeenCalled();
        expect([...revealed.values()]).toEqual([false]);
    });

    it('the iframe-only conceal a tab drag raises hides it just the same', () => {
        render(
            <PooledIframeConcealContext.Provider value={true}>
                <SandboxIframe {...props} />
            </PooledIframeConcealContext.Provider>,
        );
        expect(pool.acquire).toHaveBeenCalled();
        expect([...revealed.values()]).toEqual([false]);
    });

    it('leaves an unconcealed un-parked entry revealed', () => {
        render(<SandboxIframe {...props} />);
        expect(pool.acquire).toHaveBeenCalled();
        expect([...revealed.values()]).toEqual([true]);
    });
});

const deliver = (id: string, data: unknown) => {
    act(() => { handlers.get(id)!({ data, source: pool.getContentWindow(), origin: window.location.origin } as MessageEvent); });
};

describe('SandboxIframe motion layer', () => {
    it('its host box is a motion layer, promoted only while the scene moves, and untracked on unmount', () => {
        const { unmount } = render(<SandboxIframe {...props} />);
        expect(pool.registerMotionLayer).toHaveBeenCalledOnce();
        const [host] = pool.registerMotionLayer.mock.calls[0];
        expect(host).toBe(pool.acquire.mock.calls[0][2]);
        expect(host.className).not.toMatch(/will-change|transform-gpu/);
        unmount();
        expect(unregisterMotionLayer).toHaveBeenCalledOnce();
    });
});

describe('SandboxIframe same-poolId src change', () => {
    it('re-reveals and re-enables pointer events after a readOnly flip reloads the element', () => {
        const id = 'document:a';
        const { rerender } = render(<SandboxIframe {...props} isActiveFocus readOnly={false} />);
        deliver(id, { type: 'RENDERER_READY' });
        expect(revealed.get(id)).toBe(true);

        rerender(<SandboxIframe {...props} isActiveFocus readOnly />);
        deliver(id, { type: 'RENDERER_READY' });
        expect(revealed.get(id)).toBe(true);

        const lastRelease = Math.max(...pool.release.mock.invocationCallOrder);
        const lastPointer = Math.max(...pool.setPointerEvents.mock.invocationCallOrder);
        expect(lastPointer).toBeGreaterThan(lastRelease);
        expect(pool.setPointerEvents).toHaveBeenLastCalledWith(id, true);
    });

    it('SIBLING_WRITE uses the current readOnly, not the one captured at registration', async () => {
        const id = 'document:a';
        const { rerender } = render(<SandboxIframe {...props} readOnly={false} />);
        rerender(<SandboxIframe {...props} readOnly />);
        deliver(id, { type: 'SIBLING_WRITE', id: 1, path: 'x.json', content: '{}' });
        expect(saveSiblingFileInCell).toHaveBeenLastCalledWith(undefined, 'x.json', '{}', true);
    });
});

describe('focus blocker wheel', () => {
    it('replays a vertical wheel into the pooled iframe\'s scroller at once, and lets it bubble to select', () => {
        const frame = document.createElement('iframe');
        document.body.append(frame);
        frame.getBoundingClientRect = () => ({ left: 100, top: 50, width: 0, height: 0 } as DOMRect);
        const doc = frame.contentDocument!;
        const scroller = doc.createElement('div');
        scroller.style.overflowY = 'auto';
        Object.defineProperties(scroller, { scrollHeight: { value: 900 }, clientHeight: { value: 300 } });
        doc.body.append(scroller);
        const hitTest = vi.fn(() => scroller);
        Object.defineProperty(doc, 'elementFromPoint', { value: hitTest });
        pool.getElement.mockImplementation((id: string) => (id === 'document:a' ? frame : null));
        const selected = vi.fn();
        const frames: FrameRequestCallback[] = [];
        vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length; });
        const { getByTitle } = render(<div onWheel={selected}><SandboxIframe {...props} /></div>);

        const blocker = getByTitle('Click to Focus and Edit');
        blocker.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: 40, clientX: 130, clientY: 90 }));
        expect(hitTest).toHaveBeenCalledWith(30, 40);
        // The travel animates in from where the content stood.
        expect(frames).toHaveLength(1);
        let now = 0;
        while (frames.length) frames.shift()!(now += 1000 / 60);
        expect(scroller.scrollTop).toBe(40);
        expect(selected).toHaveBeenCalledTimes(1);

        // Sideways is the canvas swipe's: it bubbles, but scrolls nothing.
        blocker.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaX: 60, deltaY: 5 }));
        expect(frames).toHaveLength(0);
        expect(scroller.scrollTop).toBe(40);
        expect(selected).toHaveBeenCalledTimes(2);
        frame.remove();
    });
});

describe('renderer host skin', () => {
    it('the loading cover is the sheet with a teal (live) spinner, pooled and standalone (S04)', () => {
        for (const Host of [SandboxIframe, StandaloneSandboxIframe]) {
            const { container, unmount } = render(<Host {...props} />);
            const cover = container.querySelector('.pointer-events-none.absolute.inset-0') as HTMLElement;
            expect(cover.className).toContain('bg-sheet');
            expect(container.querySelector('.animate-spin')!.className).toContain('border-t-teal');
            expect(container.innerHTML).not.toMatch(RETIRED_TOKENS);
            unmount();
        }
    });
});

describe('SandboxIframe viewport culling', () => {
    it('releases the current pool entry when leaving the viewport __F0694', () => {
        let fire: ((isIntersecting: boolean) => void) | null = null;
        class ControlledObserver {
            constructor(private cb: IntersectionObserverCallback) {
                fire = (isIntersecting) => this.cb(
                    [{ isIntersecting } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
            }
            observe() {}
            unobserve() {}
            disconnect() {}
        }
        vi.stubGlobal('IntersectionObserver', ControlledObserver);
        const { rerender } = render(<SandboxIframe {...props} />);
        rerender(<SandboxIframe {...props} fileHash="h2" />);
        act(() => { fire!(false); });
        expect(pool.release).toHaveBeenCalledWith('document:a', expect.any(HTMLElement));
    });
});


describe('renderer file contract', () => {
  it('reads the exact History version and transfers numeric HTTP status __P0F02_F1703', async () => {
    const postMessage = vi.fn();
    pool.getContentWindow.mockReturnValue({ postMessage } as unknown as Window);
    const fetchMock = vi.fn(async () => new Response('', { status: 404 }));
    vi.stubGlobal('fetch', fetchMock);
    render(<SandboxIframe {...props} cellId="root" versionId="root" channel="ch" readOnly />);
    const id = [...handlers.keys()][0];
    expect(srcs.get(id)).toContain('versionId=root');
    deliver(id, { type: 'SIBLING_READ', id: 7, path: 'dir/a #%.txt' });
    await waitFor(() => expect(postMessage).toHaveBeenCalledWith({
      type: 'SIBLING_READ_RESULT', id: 7, error: 'HTTP 404', status: 404,
    }, window.location.origin));
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/channels/ch/versions/root/files/dir/a%20%23%25.txt'), undefined);
    pool.getContentWindow.mockReturnValue(null);
  });
});


it('fullscreen History reads use the same version/status contract __P0F02_F1703', async () => {
    const fetchMock = vi.fn(async () => new Response('', { status: 503 }));
    vi.stubGlobal('fetch', fetchMock);
    const { container } = render(<StandaloneSandboxIframe {...props} cellId="root"
        versionId="root" channel="ch" readOnly />);
    const frame = container.querySelector('iframe')!;
    expect(frame.src).toContain('versionId=root');
    const post = vi.spyOn(frame.contentWindow!, 'postMessage');
    act(() => window.dispatchEvent(new MessageEvent('message', {
        origin: window.location.origin, source: frame.contentWindow,
        data: { type: 'SIBLING_READ', id: 8, path: 'x.json' },
    })));
    await waitFor(() => expect(post).toHaveBeenCalledWith({
        type: 'SIBLING_READ_RESULT', id: 8, error: 'HTTP 503', status: 503,
    }, window.location.origin));
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining(
        '/channels/ch/versions/root/files/x.json'), undefined);
});


describe('logical renderer documents', () => {
    it('parks a logical document on cleanup regardless of its transport URL', () => {
        const view=render(<SandboxIframe {...props} fileHash="blob:ephemeral" />);
        view.unmount();
        expect(pool.release).toHaveBeenCalledWith('document:a',expect.any(HTMLElement));
        expect(pool.drop).not.toHaveBeenCalled();
    });
});

// An observer the test drives: intersecting on observe, then whatever
// notify() reports.
const controlledObserver = () => {
    const observer = { notify: (_isIntersecting: boolean) => {} };
    class ControlledObserver {
        constructor(callback: IntersectionObserverCallback) {
            observer.notify = (isIntersecting) => callback(
                [{ isIntersecting } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
        }
        observe() { observer.notify(true); }
        unobserve() {}
        disconnect() {}
    }
    vi.stubGlobal('IntersectionObserver', ControlledObserver);
    return observer;
};

describe('logical renderer documents on viewport cull', () => {
    it('parks a logical document off screen and un-parks the same frame, still ready, on the way back__F0716', async () => {
        const observer = controlledObserver();
        const view = render(<SandboxIframe {...props} fileHash="blob:ephemeral" />);
        const id = 'document:a';
        const cover = () => view.container.querySelector('.pointer-events-none.absolute.inset-0') as HTMLElement;
        deliver(id, { type: 'RENDERER_READY' });
        const src = pool.acquire.mock.calls.at(-1)![1];
        vi.mocked(currentWindow.postMessage).mockClear();

        await act(async () => { observer.notify(false); });
        expect(pool.release).toHaveBeenCalledWith(id, expect.any(HTMLElement));
        expect(pool.drop).not.toHaveBeenCalled();
        // A parked document keeps its state: nothing to serialize.
        expect(vi.mocked(currentWindow.postMessage).mock.calls.some(([m]) => m.type === 'FLUSH_DRAFT')).toBe(false);

        pool.acquire.mockClear();
        act(() => observer.notify(true));
        expect(pool.acquire).toHaveBeenCalledExactlyOnceWith(id, src, expect.any(HTMLElement), undefined, expect.any(Function));
        expect(pool.drop).not.toHaveBeenCalled();
        // No second RENDERER_READY needed: the loading cover stays down.
        expect(cover().style.opacity).toBe('0');

        view.unmount();
        expect(pool.release).toHaveBeenCalledWith(id, expect.any(HTMLElement));
    });

    it('recreates an evicted view and seeds it from the current document owner', async () => {
        const observer=controlledObserver();
        const view=render(<SandboxIframe {...props} fileHash="blob:first" />);
        deliver('document:a',{type:'RENDERER_READY'});
        await act(async()=>observer.notify(false));
        pool.getContentWindow.mockReturnValue(null);
        view.rerender(<SandboxIframe {...props} fileHash="blob:replacement" fileContent="latest dirty body" />);
        pool.acquire.mockClear();
        act(()=>observer.notify(true));
        expect(pool.acquire).toHaveBeenCalledOnce();
        expect(pool.acquire.mock.calls[0][0]).toBe('document:a');
        expect(pool.acquire.mock.calls[0][1]).not.toContain('blob');
        pool.getContentWindow.mockReturnValue(currentWindow);
        deliver('document:a',{type:'RENDERER_READY'});
        deliver('document:a',{type:'GET_DOCUMENT',viewId:'parked-frame',id:42});
        expect(currentWindow.postMessage).toHaveBeenCalledWith(expect.objectContaining({type:'PREVIEW_PRIMITIVE_RESULT',id:42,value:expect.objectContaining({content:'latest dirty body'})}),window.location.origin);
        expect(pool.drop).not.toHaveBeenCalled();
    });

});


describe('parent compiler host wiring', () => {
    it('pooled host delivers bundle code with the owning shell token__F0251_F1868', async () => {
        const postMessage = vi.fn();
        const target = { postMessage } as unknown as Window;
        pool.getContentWindow.mockReturnValue(target);
        vi.stubGlobal('fetch', vi.fn(async () => new Response('pooled module')));
        render(<SandboxIframe {...props} rendererHash={'a'.repeat(64)} />);
        const id = [...handlers.keys()][0];
        const token = 'd'.repeat(32);
        act(() => handlers.get(id)!({ origin: window.location.origin, source: target,
            data: { type: 'RENDERER_BUNDLE_REQUEST', token, rendererId: 'r', rendererHash: 'a'.repeat(64) },
        } as MessageEvent));
        await waitFor(() => expect(postMessage).toHaveBeenCalledWith(
            { type: 'RENDERER_BUNDLE_RESULT', token, code: 'pooled module' }, window.location.origin));
        pool.getContentWindow.mockReturnValue(null);
    });
    it('standalone host delivers bundle code only for its own shell__F0251_F1868', async () => {
        const fetch = vi.fn(async () => new Response('standalone module'));
        vi.stubGlobal('fetch', fetch);
        const { container } = render(<StandaloneSandboxIframe {...props} rendererHash={'b'.repeat(64)} />);
        const target = container.querySelector('iframe')!.contentWindow!;
        const post = vi.spyOn(target, 'postMessage');
        const token = 'e'.repeat(32);
        const data = { type: 'RENDERER_BUNDLE_REQUEST', token, rendererId: 'r', rendererHash: 'b'.repeat(64) };
        act(() => window.dispatchEvent(new MessageEvent('message', { origin: window.location.origin, source: window, data })));
        expect(fetch).not.toHaveBeenCalled();
        act(() => window.dispatchEvent(new MessageEvent('message', { origin: window.location.origin, source: target, data })));
        await waitFor(() => expect(post).toHaveBeenCalledWith(
            { type: 'RENDERER_BUNDLE_RESULT', token, code: 'standalone module' }, window.location.origin));
    });
});


describe('editing preferences forwarding', () => {
    afterEach(() => applyEditingSnapshot({ spellcheck: true }));
    const editingPosts = (post: ReturnType<typeof vi.fn>) =>
        post.mock.calls.map(([m]) => m).filter((m: { type?: string }) => m?.type === 'SET_EDITING');

    it('the pooled host sends SET_EDITING once the renderer is ready, and again on every change', () => {
        applyEditingSnapshot({ spellcheck: true });
        render(<SandboxIframe {...props} />);
        const post = vi.mocked(currentWindow.postMessage);
        expect(editingPosts(post)).toEqual([]);
        deliver('document:a', { type: 'RENDERER_READY' });
        expect(editingPosts(post)).toEqual([{ type: 'SET_EDITING', spellcheck: true }]);
        act(() => applyEditingSnapshot({ spellcheck: false }));
        expect(editingPosts(post)).toEqual([
            { type: 'SET_EDITING', spellcheck: true }, { type: 'SET_EDITING', spellcheck: false },
        ]);
        expect(post).toHaveBeenLastCalledWith({ type: 'SET_EDITING', spellcheck: false }, window.location.origin);
    });

    it('the standalone host sends SET_EDITING on load, once ready, and on every change', () => {
        applyEditingSnapshot({ spellcheck: false });
        const { container } = render(<StandaloneSandboxIframe {...props} />);
        const frame = container.querySelector('iframe')!;
        const post = vi.spyOn(frame.contentWindow!, 'postMessage').mockImplementation(() => {});
        act(() => { frame.dispatchEvent(new Event('load')); });
        expect(editingPosts(post as never)).toEqual([{ type: 'SET_EDITING', spellcheck: false }]);
        act(() => { window.dispatchEvent(new MessageEvent('message', { origin: window.location.origin, source: frame.contentWindow, data: { type: 'RENDERER_READY' } })); });
        expect(editingPosts(post as never)).toHaveLength(2);
        act(() => applyEditingSnapshot({ spellcheck: true }));
        expect(editingPosts(post as never).at(-1)).toEqual({ type: 'SET_EDITING', spellcheck: true });
        expect(editingPosts(post as never)).toHaveLength(3);
    });
});

describe('keyboard forwarding hosts __F0479_F0913',()=>{
 it('pooled host delivers physical code and modifier release',()=>{
  render(<SandboxIframe {...props}/>);const down=vi.fn();const up=vi.fn();window.addEventListener('keydown',down);window.addEventListener('keyup',up);
  try {deliver('document:a',{type:'FORWARD_KEYDOWN',key:'a',code:'KeyA',altKey:true});deliver('document:a',{type:'FORWARD_KEYUP',key:'Alt',code:'AltLeft'});
   expect(down.mock.calls[0][0].code).toBe('KeyA');expect(up.mock.calls[0][0].key).toBe('Alt');
  }finally{window.removeEventListener('keydown',down);window.removeEventListener('keyup',up);}
 });
 it('standalone host accepts release only from its own frame',()=>{
  const {container}=render(<StandaloneSandboxIframe {...props}/>);const frame=container.querySelector('iframe')!;const up=vi.fn();window.addEventListener('keyup',up);
  try {window.dispatchEvent(new MessageEvent('message',{origin:window.location.origin,source:window,data:{type:'FORWARD_KEYUP',key:'Meta'}}));expect(up).not.toHaveBeenCalled();
   window.dispatchEvent(new MessageEvent('message',{origin:window.location.origin,source:frame.contentWindow,data:{type:'FORWARD_KEYUP',key:'Meta',code:'MetaLeft'}}));expect(up.mock.calls[0][0].key).toBe('Meta');
  }finally{window.removeEventListener('keyup',up);}
 });
});


it('releases an exiting view while retaining its logical owner',async()=>{
  const view=render(<SandboxIframe {...props} />);
  presence.present=false;
  view.rerender(<SandboxIframe {...props} />);
  await act(async()=>{});
  expect(pool.drop).not.toHaveBeenCalled();
  expect(pool.release).toHaveBeenCalledWith('document:a',expect.any(HTMLElement));
  view.unmount();
});

it('refreshes the pooled display alias while keeping the real owner in its URL __D11', () => {
  setRendererHandlers([{types:[{ext:'.report_html',render_as:'.html'}]}]);
  render(<SandboxIframe {...props} fileName="folder/page.report_html" />);
  const latest=()=>new URL(pool.acquire.mock.calls.at(-1)![1],window.location.href);
  expect(latest().searchParams.get('filename')).toBe('folder/page.report_html');
  expect(latest().searchParams.get('displayName')).toBe('folder/page.html');
  act(()=>setRendererHandlers([{types:[{ext:'.report_html',render_as:'.md'}]}]));
  expect(latest().searchParams.get('filename')).toBe('folder/page.report_html');
  expect(latest().searchParams.get('displayName')).toBe('folder/page.md');
});

describe('renderer flush belongs to the physical document',()=>{
    for(const standalone of [false,true]){
        it(`${standalone?'standalone':'pooled'} accepts a held flush after the same owner's accepted body hash changes`,async()=>{
            const Host=standalone?StandaloneSandboxIframe:SandboxIframe;
            const initial={...props,cellId:'flush-cell',fileHash:'before-save'};
            const view=render(<Host {...initial}/>);
            const iframe=view.container.querySelector('iframe');
            const target=standalone?(iframe as HTMLIFrameElement).contentWindow!:currentWindow;
            const post=vi.spyOn(target,'postMessage').mockImplementation(()=>{});
            const {flushRendererEditors}=await import('@/lib/rendererFlush');
            const pending=flushRendererEditors('flush-cell','a.md');
            await waitFor(()=>expect(post.mock.calls.some(([message])=>message.type==='FLUSH_DRAFT')).toBe(true));
            const request=post.mock.calls.find(([message])=>message.type==='FLUSH_DRAFT')![0];
            view.rerender(<Host {...initial} fileHash="accepted-save"/>);
            if(standalone)expect(view.container.querySelector('iframe')).toBe(iframe);
            else expect(pool.getContentWindow()).toBe(target);
            await act(async()=>{window.dispatchEvent(new MessageEvent('message',{origin:window.location.origin,source:target,data:{type:'FLUSH_DRAFT_ACK',requestId:request.requestId}}));await pending;});
            post.mockRestore();
        });
        it(`${standalone?'standalone':'pooled'} rejects an old acknowledgement after actual physical replacement`,async()=>{
            const Host=standalone?StandaloneSandboxIframe:SandboxIframe;
            const initial={...props,cellId:'flush-cell',fileHash:'same-body'};
            const view=render(<Host {...initial}/>);
            const target=standalone?(view.container.querySelector('iframe') as HTMLIFrameElement).contentWindow!:currentWindow;
            const post=vi.spyOn(target,'postMessage').mockImplementation(()=>{});
            const {flushRendererEditors}=await import('@/lib/rendererFlush');
            const pending=flushRendererEditors('flush-cell','a.md').then(()=>null,error=>String(error.message));
            await waitFor(()=>expect(post.mock.calls.some(([message])=>message.type==='FLUSH_DRAFT')).toBe(true));
            const request=post.mock.calls.find(([message])=>message.type==='FLUSH_DRAFT')![0];
            if(standalone)view.rerender(<Host {...initial} documentId="different-owner"/>);
            else pool.getContentWindow.mockReturnValue({postMessage:vi.fn()} as unknown as Window);
            window.dispatchEvent(new MessageEvent('message',{origin:window.location.origin,source:target,data:{type:'FLUSH_DRAFT_ACK',requestId:request.requestId}}));
            expect(await pending).toContain('Renderer changed before acknowledging');post.mockRestore();
        });
    }
});

describe('annotation responders in both iframe hosts', () => {
    const CONTEXT = { label: 'a.md › Heading', text: 'The heading' };
    for (const standalone of [false, true]) {
        const Host = standalone ? StandaloneSandboxIframe : SandboxIframe;
        const name = standalone ? 'standalone' : 'pooled';
        const frameOf = (view: ReturnType<typeof render>) => standalone
            ? (view.container.querySelector('iframe') as HTMLIFrameElement).contentWindow!
            : currentWindow;
        const frameSays = (target: Window, data: Record<string, unknown>) => act(() => {
            if (standalone) window.dispatchEvent(new MessageEvent('message', { origin: window.location.origin, source: target, data }));
            else deliver(props.documentId, data);
        });

        it(`${name}: the view registers, learns the frame's capability and answers over the wire`, async () => {
            const view = render(<Host {...props} cellId="note-cell" isActiveFocus />);
            const target = frameOf(view);
            const post = vi.spyOn(target, 'postMessage').mockImplementation((message: { type?: string; requestId?: string }) => {
                if (message?.type !== 'ANNOTATION_REQUEST') return;
                queueMicrotask(() => {
                    for (const data of [{ type: 'ANNOTATION_ACK', requestId: message.requestId },
                        { type: 'ANNOTATION_RESULT', requestId: message.requestId, context: CONTEXT }]) {
                        window.dispatchEvent(new MessageEvent('message', { origin: window.location.origin, source: target, data }));
                    }
                });
            });
            const [responder] = annotationResponders('note-cell', 'a.md');
            expect(responder.rendererId).toBe('r');
            expect([responder.capable(), responder.canInteract(), responder.focused(), responder.visible()]).toEqual([false, true, true, true]);
            frameSays(target, { type: 'ANNOTATION_CAPABLE', capable: true });
            expect(responder.capable()).toBe(true);
            await expect(requestAnnotation('note-cell', 'a.md', 'selection')).resolves.toEqual({
                status: 'ok', path: 'a.md', rendererId: 'r', context: CONTEXT });
            expect(post).toHaveBeenCalledWith({ type: 'ANNOTATION_REQUEST', requestId: expect.any(String), mode: 'selection' }, window.location.origin);
            frameSays(target, { type: 'ANNOTATION_CAPABLE', capable: false });
            expect(responder.capable()).toBe(false);
            view.unmount();
            expect(responder.capable()).toBe(false);
            expect(annotationResponders('note-cell', 'a.md')).toEqual([]);
            post.mockRestore();
        });

        it(`${name}: its view reads the frame document's selection, and nothing once the frame is gone`, () => {
            const view = render(<Host {...props} cellId="note-cell" isActiveFocus />);
            const target = frameOf(view);
            const [responder] = annotationResponders('note-cell', 'a.md');
            const own = Object.getOwnPropertyDescriptor(target, 'document');
            const doc = target.document ?? document.implementation.createHTMLDocument();
            if (!target.document) Object.defineProperty(target, 'document', { value: doc, configurable: true });
            const getSelection = vi.spyOn(doc, 'getSelection');
            try {
                getSelection.mockReturnValue({ isCollapsed: false, anchorNode: doc, toString: () => 'frame words' } as unknown as Selection);
                expect(responder.selection()).toBe('frame words');
                getSelection.mockReturnValue({ isCollapsed: true, anchorNode: doc, toString: () => '' } as unknown as Selection);
                expect(responder.selection()).toBe('');
                getSelection.mockReturnValue({ isCollapsed: false, anchorNode: doc, toString: () => 'frame words' } as unknown as Selection);
                view.unmount();
                expect(responder.selection()).toBe('');
            } finally {
                getSelection.mockRestore();
                if (own) Object.defineProperty(target, 'document', own); else delete (target as { document?: Document }).document;
            }
        });

        it(`${name}: a view that goes away mid-request cancels it in the frame, which outlives it`, async () => {
            const view = render(<Host {...props} cellId="note-cell" isActiveFocus />);
            const target = frameOf(view);
            const post = vi.spyOn(target, 'postMessage').mockImplementation((message: { type?: string; requestId?: string }) => {
                if (message?.type !== 'ANNOTATION_REQUEST') return;
                queueMicrotask(() => window.dispatchEvent(new MessageEvent('message', {
                    origin: window.location.origin, source: target, data: { type: 'ANNOTATION_ACK', requestId: message.requestId } })));
            });
            frameSays(target, { type: 'ANNOTATION_CAPABLE', capable: true });
            const outcome = requestAnnotation('note-cell', 'a.md', 'selection');
            await waitFor(() => expect(post.mock.calls.some(([m]) => m.type === 'ANNOTATION_REQUEST')).toBe(true));
            const { requestId } = post.mock.calls.find(([m]) => m.type === 'ANNOTATION_REQUEST')![0];
            view.unmount();
            await expect(outcome).resolves.toMatchObject({ status: 'unavailable', reason: 'parked' });
            expect(post).toHaveBeenCalledWith({ type: 'ANNOTATION_CANCEL', requestId }, window.location.origin);
            post.mockRestore();
        });

        it(`${name}: a read-only, History or diff view takes no requests, and a cell-less one registers none`, () => {
            const locked = render(<Host {...props} cellId="note-cell" readOnly />);
            expect(annotationResponders('note-cell', 'a.md').map(r => r.canInteract())).toEqual([false]);
            locked.unmount();
            const history = render(<Host {...props} cellId="note-cell" versionId="v1" />);
            expect(annotationResponders('note-cell', 'a.md').map(r => r.canInteract())).toEqual([false]);
            history.unmount();
            const diff = render(<Host {...props} cellId="note-cell" diffHash="d" />);
            expect(annotationResponders('note-cell', 'a.md').map(r => r.canInteract())).toEqual([false]);
            diff.unmount();
            render(<Host {...props} />);
            expect(annotationResponders('', 'a.md')).toEqual([]);
        });
    }

    it('pooled: a new frame starts incapable until it says otherwise, and a lost frame answers parked', async () => {
        const view = render(<SandboxIframe {...props} cellId="note-cell" isActiveFocus />);
        deliver('document:a', { type: 'ANNOTATION_CAPABLE', capable: true });
        expect(annotationResponders('note-cell', 'a.md')[0].capable()).toBe(true);
        view.rerender(<SandboxIframe {...props} documentId="document:b" cellId="note-cell" isActiveFocus />);
        const [fresh] = annotationResponders('note-cell', 'a.md');
        expect(fresh.capable()).toBe(false);
        pool.getContentWindow.mockReturnValue(null);
        await expect(fresh.request({ id: 'r1', mode: 'pick' }, new AbortController().signal)).rejects.toMatchObject({
            name: 'AnnotationUnavailable', reason: 'parked' });
    });
});

import { expect, it, vi } from 'vitest';
import { useState } from 'react';
import { render } from 'vitest-browser-react';
import { page } from 'vitest/browser';
import { NativeRenderer } from '../NativeRenderer';
import { FIXTURE_SCRIM_ID, FIXTURE_WHEEL_ID } from '../../test/rendererFixtures';
import '@/app/globals.css';

// Real host, served renderer module, shadow hit-testing and pointer input.
// The wrapper supplies the same focus-owner state transition as a cell.
it('first scrim click focuses and acts once at the same shadow coordinates __F1920', async () => {
  const changes: unknown[] = [];
  const releases: { x: number; y: number; trusted: boolean }[] = [];
  function Cell() {
    const [active, setActive] = useState(false);
    return <div style={{ width: 500, height: 240 }} data-active={active}
      onMouseUp={event => {
        releases.push({ x: event.clientX, y: event.clientY, trusted: event.isTrusted });
        setActive(true);
      }}>
      <NativeRenderer rendererId={FIXTURE_SCRIM_ID} fileName="scrim.txt"
        fileContent="0" isActiveFocus={active} onContentChange={body=>changes.push(body)} />
    </div>;
  }
  const view = await render(<Cell />);
  try {
    const action = page.getByRole('button', { name: 'Renderer action' });
    await expect.element(action).toBeVisible();
    const button = action.element() as HTMLButtonElement;
    expect(button.getRootNode()).toBeInstanceOf(ShadowRoot);
    const scrim = page.getByTitle('Click to focus');
    const rect = button.getBoundingClientRect();
    const overlay = scrim.element().getBoundingClientRect();
    const point = { x: Math.floor(rect.left) + 15, y: Math.floor(rect.top) + 15 };
    expect(document.elementFromPoint(point.x, point.y)).toBe(scrim.element());
    await scrim.click({ position: { x: point.x - overlay.left, y: point.y - overlay.top } });
    await vi.waitFor(() => expect(changes).toEqual([
      '1',
    ]));
    await expect.element(scrim).not.toBeInTheDocument();
    expect(releases).toHaveLength(1);
    expect(releases[0].trusted).toBe(true);
    expect(releases[0].x).toBeGreaterThan(rect.left);
    expect(releases[0].x).toBeLessThan(rect.right);
    expect(releases[0].y).toBeGreaterThan(rect.top);
    expect(releases[0].y).toBeLessThan(rect.bottom);
    expect(JSON.parse(button.dataset.event!)).toEqual({ clicks: 1, ...releases[0],
      trusted: false, bubbles: true, composed: true, cancelable: true });
    expect(action.element()).toBe(button);
    await action.click();
    expect(JSON.parse(button.dataset.event!)).toMatchObject({ clicks: 2, trusted: true });
    expect(changes).toHaveLength(2);
  } finally {
    await view.unmount();
  }
});

it('first scrim wheel focuses and scrolls the shadow scroller by its travel, once', async () => {
  const wheels: { trusted: boolean; deltaY: number }[] = [];
  function Cell() {
    const [active, setActive] = useState(false);
    // The card's rule: a wheel over the unfocused cell focuses it.
    return <div style={{ width: 500, height: 240 }}
      onWheel={event => {
        wheels.push({ trusted: event.isTrusted, deltaY: event.deltaY });
        setActive(true);
      }}>
      <NativeRenderer rendererId={FIXTURE_WHEEL_ID} fileName="wheel.txt"
        fileContent="" isActiveFocus={active} />
    </div>;
  }
  const view = await render(<Cell />);
  try {
    const host = document.querySelector('[data-native-renderer] > div') as HTMLElement;
    await vi.waitFor(() => expect(host.shadowRoot?.querySelector('[data-wheel-column]')).toBeTruthy());
    const column = host.shadowRoot!.querySelector('[data-wheel-column]') as HTMLElement;
    expect(column.scrollHeight).toBeGreaterThan(column.clientHeight);
    const scrim = page.getByTitle('Click to focus');
    await scrim.wheel({ delta: { y: 100 } });
    await expect.element(scrim).not.toBeInTheDocument();
    await vi.waitFor(() => expect(column.scrollTop).toBe(100));
    // One replay, not one per frame: it stays put.
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    expect(column.scrollTop).toBe(100);
    expect(wheels).toEqual([{ trusted: true, deltaY: 100 }]);
  } finally {
    await view.unmount();
  }
});

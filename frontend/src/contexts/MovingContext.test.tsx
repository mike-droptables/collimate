import { act, renderHook } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { MovingProvider, useMoving } from './MovingContext';

it('disables iframe interaction for the pan interval and removes both listeners on unmount (F0700)', () => {
    const remove = vi.spyOn(window, 'removeEventListener');
    const { result, unmount } = renderHook(useMoving, { wrapper: MovingProvider });
    expect(result.current.isMoving).toBe(false);
    act(() => { window.dispatchEvent(new Event('canvas-pan-start')); });
    expect(result.current.isMoving).toBe(true);
    act(() => { window.dispatchEvent(new Event('canvas-pan-end')); });
    expect(result.current.isMoving).toBe(false);
    unmount();
    expect(remove).toHaveBeenCalledWith('canvas-pan-start', expect.any(Function));
    expect(remove).toHaveBeenCalledWith('canvas-pan-end', expect.any(Function));
    remove.mockRestore();
});

/**
 * @vitest-environment jsdom
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import {
  isCellOccluded,
  registerGuestOccluder,
  resetGuestOcclusion,
  subscribeGuestOcclusion,
  useGuestOccluder,
} from '../guestOcclusion';

afterEach(() => {
  resetGuestOcclusion();
});

describe('the registry', () => {
  it('starts empty — nothing is occluded', () => {
    expect(isCellOccluded('cell-1')).toBe(false);
    expect(isCellOccluded(null)).toBe(false);
  });

  it("'all' covers every guest, cell-less ones included", () => {
    const remove = registerGuestOccluder('all');
    expect(isCellOccluded('cell-1')).toBe(true);
    expect(isCellOccluded(null)).toBe(true);
    remove();
    expect(isCellOccluded('cell-1')).toBe(false);
  });

  it('cellIds covers only the cells it names', () => {
    registerGuestOccluder({ cellIds: ['cell-1'] });
    expect(isCellOccluded('cell-1')).toBe(true);
    expect(isCellOccluded('cell-2')).toBe(false);
    // A guest with no cell can't be the one this overlay sits over.
    expect(isCellOccluded(null)).toBe(false);
  });

  it('allExcept covers everything but the cells it spares', () => {
    registerGuestOccluder({ allExcept: ['cell-1'] });
    expect(isCellOccluded('cell-1')).toBe(false);
    expect(isCellOccluded('cell-2')).toBe(true);
    expect(isCellOccluded(null)).toBe(true);
  });

  it('scopes stack — a cell is clear only when no occluder covers it', () => {
    const menu = registerGuestOccluder('all');
    const overlay = registerGuestOccluder({ cellIds: ['cell-1'] });
    expect(isCellOccluded('cell-1')).toBe(true);
    menu();
    expect(isCellOccluded('cell-1')).toBe(true);
    expect(isCellOccluded('cell-2')).toBe(false);
    overlay();
    expect(isCellOccluded('cell-1')).toBe(false);
  });

  it('notifies subscribers on every register and deregister, and only then', () => {
    const listener = vi.fn();
    const unsubscribe = subscribeGuestOcclusion(listener);

    const remove = registerGuestOccluder('all');
    expect(listener).toHaveBeenCalledTimes(1);
    remove();
    expect(listener).toHaveBeenCalledTimes(2);
    remove(); // double deregister is a no-op, not a notification
    expect(listener).toHaveBeenCalledTimes(2);

    unsubscribe();
    registerGuestOccluder('all');
    expect(listener).toHaveBeenCalledTimes(2);
  });
});

describe('useGuestOccluder', () => {
  it('registers while active and deregisters on unmount', () => {
    const { unmount } = renderHook(() => useGuestOccluder('all'));
    expect(isCellOccluded('cell-1')).toBe(true);
    unmount();
    expect(isCellOccluded('cell-1')).toBe(false);
  });

  it('follows the active flag — the open/closed state of the overlay', () => {
    const { rerender } = renderHook(({ open }) => useGuestOccluder('all', open), {
      initialProps: { open: false },
    });
    expect(isCellOccluded(null)).toBe(false);
    rerender({ open: true });
    expect(isCellOccluded(null)).toBe(true);
    rerender({ open: false });
    expect(isCellOccluded(null)).toBe(false);
  });

  it('keys the scope by value, so a per-render array does not churn', () => {
    const listener = vi.fn();
    subscribeGuestOcclusion(listener);
    const { rerender } = renderHook(
      ({ ids }) => useGuestOccluder({ cellIds: ids }),
      { initialProps: { ids: ['cell-1'] } },
    );
    expect(listener).toHaveBeenCalledTimes(1);

    // Same cells, new array identity: no re-registration.
    rerender({ ids: ['cell-1'] });
    expect(listener).toHaveBeenCalledTimes(1);

    // Actually different cells: re-registered.
    rerender({ ids: ['cell-2'] });
    expect(isCellOccluded('cell-2')).toBe(true);
    expect(isCellOccluded('cell-1')).toBe(false);
  });
});

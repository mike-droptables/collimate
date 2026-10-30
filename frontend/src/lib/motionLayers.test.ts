// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { createMotionLayers, promoteForAnimation } from './motionLayers';

const promotedClasses = (el: HTMLElement) =>
  ['transform-gpu', 'will-change-transform'].filter(name => el.classList.contains(name));

describe('motion layers', () => {
  it('promote only while the scene moves, and leave the class list as they found it at rest', () => {
    const layers = createMotionLayers();
    const a = document.createElement('div');
    a.className = 'absolute inset-0';
    const b = document.createElement('div');
    layers.track(a);
    layers.track(b);
    // At rest a tracked element carries no promotion at all.
    expect(promotedClasses(a)).toEqual([]);
    expect(layers.promoted()).toBe(false);

    layers.promote();
    expect(layers.promoted()).toBe(true);
    expect(promotedClasses(a)).toEqual(['transform-gpu', 'will-change-transform']);
    expect(promotedClasses(b)).toEqual(['transform-gpu', 'will-change-transform']);

    // The classes come off, not just the property: the element's own
    // classes are all that is left.
    layers.demote();
    expect(layers.promoted()).toBe(false);
    expect(a.className).toBe('absolute inset-0');
    expect(b.className).toBe('');
  });

  it('an element tracked mid-motion joins the promotion at once', () => {
    const layers = createMotionLayers();
    layers.promote();
    const late = document.createElement('div');
    layers.track(late);
    expect(promotedClasses(late)).toEqual(['transform-gpu', 'will-change-transform']);
    layers.demote();
    expect(promotedClasses(late)).toEqual([]);
  });

  it('an untracked element is demoted and left alone by later motion', () => {
    const layers = createMotionLayers();
    const el = document.createElement('div');
    layers.track(el);
    layers.promote();
    layers.untrack(el);
    expect(promotedClasses(el)).toEqual([]);
    layers.demote();
    layers.promote();
    expect(promotedClasses(el)).toEqual([]);
  });

  it('repeated promotes within one motion touch the DOM once', () => {
    const layers = createMotionLayers();
    const el = document.createElement('div');
    layers.track(el);
    const toggle = vi.spyOn(el.classList, 'toggle');
    // Every camera value change of a pan promotes again.
    for (let frame = 0; frame < 30; frame++) layers.promote();
    expect(toggle).toHaveBeenCalledTimes(2);
    layers.demote();
    layers.demote();
    expect(toggle).toHaveBeenCalledTimes(4);
  });
});

describe('promoteForAnimation', () => {
  it('hints the element for its own animation and clears the hint at its end, leaving the motion-layer classes alone', () => {
    const el = document.createElement('div');
    const layers = createMotionLayers();
    layers.track(el);
    layers.promote();
    promoteForAnimation(el, true);
    expect(el.style.willChange).toBe('transform, opacity');
    promoteForAnimation(el, false);
    expect(el.style.willChange).toBe('');
    // The camera's promotion is independent of it.
    expect(el.classList.contains('will-change-transform')).toBe(true);
    layers.demote();
    expect(el.className).toBe('');
    // An unmounted element is a no-op.
    expect(() => promoteForAnimation(null, true)).not.toThrow();
  });
});

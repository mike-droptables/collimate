import { describe, it, expect, vi } from "vitest";
import { registerCellSaveBarrier, flushCellSaves } from "../cellSaveBarrier";

describe("cellSaveBarrier — editor flushers", () => {
  it("flushes only the named cell's editors", async () => {
    const a = vi.fn();
    const b = vi.fn();
    const offA = registerCellSaveBarrier("cell-a", a);
    const offB = registerCellSaveBarrier("cell-b", b);
    await flushCellSaves("cell-a");
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).not.toHaveBeenCalled();
    offA();
    offB();
  });

  it("flushes every cell without a cell id, and awaits async flushers", async () => {
    let release!: () => void;
    const slow = vi.fn(() => new Promise<void>((r) => { release = r; }));
    const quick = vi.fn();
    const offSlow = registerCellSaveBarrier("cell-a", slow);
    const offQuick = registerCellSaveBarrier("cell-b", quick);
    const settled = vi.fn();
    const done = flushCellSaves().then(settled);
    await Promise.resolve();
    expect(quick).toHaveBeenCalledTimes(1);
    expect(settled).not.toHaveBeenCalled();
    release();
    await done;
    expect(settled).toHaveBeenCalled();
    offSlow();
    offQuick();
  });

  it("a deregistered editor is not flushed; an unknown cell is a no-op", async () => {
    const fn = vi.fn();
    const off = registerCellSaveBarrier("cell-c", fn);
    off();
    await flushCellSaves("cell-c");
    await flushCellSaves("cell-unknown");
    expect(fn).not.toHaveBeenCalled();
  });
});

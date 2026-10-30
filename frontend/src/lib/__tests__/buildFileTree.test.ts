import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect } from "vitest";
import { buildFileTree } from "@/lib/fileTree";
import type { VirtualFile } from "@/lib/types";

const mk = (name: string): VirtualFile => (fileFixture(name, "", {language: "text", timestamp: "2024-01-01T00:00:00Z"}));

describe("buildFileTree", () => {
  it("returns an empty root for empty input", () => {
    expect(buildFileTree([])).toEqual({});
  });

  it("puts flat files at the root and tags the fileIndex", () => {
    const files = [mk("a.md"), mk("b.ts")];
    const tree = buildFileTree(files);
    expect(Object.keys(tree).sort()).toEqual(["a.md", "b.ts"]);
    expect(tree["a.md"].type).toBe("file");
    expect(tree["a.md"].fileIndex).toBe(0);
    expect(tree["b.ts"].fileIndex).toBe(1);
  });

  it("creates intermediate folders for nested paths", () => {
    const tree = buildFileTree([mk("src/components/App.tsx")]);
    expect(tree["src"].type).toBe("folder");
    expect(tree["src"].children["components"].type).toBe("folder");
    const leaf = tree["src"].children["components"].children["App.tsx"];
    expect(leaf.type).toBe("file");
    expect(leaf.path).toBe("src/components/App.tsx");
  });

  it("reuses an existing folder when sibling files are added", () => {
    const tree = buildFileTree([mk("src/a.ts"), mk("src/b.ts")]);
    expect(Object.keys(tree["src"].children).sort()).toEqual(["a.ts", "b.ts"]);
  });

  it("normalizes backslashes to forward slashes", () => {
    const tree = buildFileTree([mk("win\\sub\\file.md")]);
    expect(tree["win"].children["sub"].children["file.md"].type).toBe("file");
  });

  it("preserves fileIndex order from the input list", () => {
    const tree = buildFileTree([mk("x/a.md"), mk("x/b.md"), mk("c.md")]);
    expect(tree["x"].children["a.md"].fileIndex).toBe(0);
    expect(tree["x"].children["b.md"].fileIndex).toBe(1);
    expect(tree["c.md"].fileIndex).toBe(2);
  });
});

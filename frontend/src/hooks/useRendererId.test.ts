import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useEffect } from "react";

// The module fetches /api/renderers + /api/renderer_preferences eagerly on
// import and caches at module scope, so each scenario stubs fetch first and
// imports a fresh copy.
type Manifest = {
  id: string;
  name?: string;
  extensions?: string[];
  default_for?: string[];
  supports_diff?: boolean;
  embed?: string;
  content_hash?: string;
};

type Handler = { types: Array<{ext:string;render_as?:string}>, clashes?:string[], disabled?:boolean };
function stubFetch(manifests: Manifest[], prefs: Record<string, string>, handlers:Handler[] = []) {
  const impl = vi.fn(async (url: unknown) => ({
    ok: true,
    json: async () =>
      String(url).includes("renderer_preferences") ? prefs : String(url).endsWith('/handlers') ? handlers : manifests,
  }));
  vi.stubGlobal("fetch", impl);
  return impl;
}

async function loadModule(manifests: Manifest[], prefs: Record<string, string>, handlers:Handler[] = []) {
  vi.resetModules();
  stubFetch(manifests, prefs, handlers);
  const mod = await import("./useRendererId");
  // The eager refresh may still be in flight; an explicit refresh after it
  // settles guarantees the cache reflects our stub before assertions.
  await new Promise((r) => setTimeout(r, 0));
  await mod.refreshRendererCache();
  return mod;
}

// The app's built-in text renderer is in every registry, a default for every
// text file; the server lists the built-ins after the channel's renderers.
const TEXT: Manifest = { id: "text", name: "Text Editor", extensions: [".txt", ".log"], default_for: ["*"], supports_diff: true, embed: "native", content_hash: "t1" };
// The image and PDF viewers are found by what they are the default for, never
// by their ids.
const USER_MANIFESTS: Manifest[] = [
  { id: "schema_form", name: "Schema Form", extensions: [".set_yaml", ".generator", ".renderer"], supports_diff: false, embed: "native", content_hash: "h1" },
  { id: "code-pro", name: "Code Pro", extensions: [".ts"], supports_diff: true, embed: "iframe" },
  { id: "pictures", name: "Image Viewer", extensions: [".png"], default_for: [".png"], embed: "native" },
  { id: "papers", name: "PDF", extensions: [".pdf"], default_for: [".pdf"], embed: "native" },
];
const MANIFESTS: Manifest[] = [...USER_MANIFESTS, TEXT];

describe("resolveRenderer", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it('applies the longest handler alias once in resolution and Open with __D11', async()=>{
    const mod=await loadModule([
      {id:'html',extensions:['.html']},{id:'markdown',extensions:['.md']},
    ],{},[{types:[{ext:'.page_html',render_as:'.html'},{ext:'.html',render_as:'.md'},{ext:'.special.page_html',render_as:'.md'}]}]);
    expect(mod.resolveRenderer('name.PAGE_HTML').id).toBe('html');
    expect(mod.resolveRenderer('name.special.PAGE_HTML').id).toBe('markdown');
    expect(mod.rendererCandidatesFor('name.page_html').map(r=>r.id)).toContain('html');
    expect(mod.getSupportedExtensions().find(x=>x.ext==='.page_html')?.renderers).toContain('html');
  });

  it('preserves real-extension preferences and refreshes aliases independently __D11',async()=>{
    const manifests=[{id:'html',extensions:['.html']},{id:'markdown',extensions:['.md']}];
    const mod=await loadModule(manifests,{'.page_html':'markdown'},[{types:[{ext:'.page_html',render_as:'.html'}]}]);
    expect(mod.resolveRenderer('name.page_html').id).toBe('markdown');
    stubFetch(manifests,{},[{types:[{ext:'.page_html',render_as:'.html'}]}]);
    await mod.refreshRendererCache();
    expect(mod.resolveRenderer('name.page_html').id).toBe('html');
    stubFetch(manifests,{},[{types:[{ext:'.page_html',render_as:'.md'}]}]);
    await mod.refreshRendererCache();
    expect(mod.resolveRenderer('name.page_html').id).toBe('markdown');
  });

  it("mounts app-owned definition viewers while preserving raw overrides__D13", async () => {
    const mod = await loadModule(MANIFESTS, {});
    for (const [ext, kind] of [['renderer','renderer'], ['generator','generator'], ['runner','runner'], ['handler','handler'], ['options_stack','options']]) {
      expect(mod.resolveRenderer(`package/file.${ext}`).id).toBe(`definition-${kind}`);
      expect(mod.resolveRendererWithOverride(`package/file.${ext}`, 'text').id).toBe('text');
    }
  });

  it("maps registered extensions to their renderer", async () => {
    const { resolveRenderer } = await loadModule(MANIFESTS, {});
    expect(resolveRenderer("settings.set_yaml").id).toBe("schema_form");
    expect(resolveRenderer("settings.set_yaml").embed).toBe("native");
    expect(resolveRenderer("main.ts").id).toBe("code-pro");
    expect(resolveRenderer("generator/custom.generator").id).toBe("definition-generator");
    expect(resolveRenderer("display/custom.renderer").id).toBe("definition-renderer");
  });

  it("prefers the LONGEST matching registry extension, whatever the registry order", async () => {
    // `.slides.md` is a more specific claim than `.md` — the compound-extension
    // renderer wins even when the generic one is listed (and so indexed) first.
    const manifests: Manifest[] = [
      { id: "generic-md", name: "Generic MD", extensions: [".md"], embed: "iframe" },
      { id: "deck", name: "Deck", extensions: [".slides.md", ".slides"], embed: "iframe" },
    ];
    const { resolveRenderer } = await loadModule(manifests, {});
    expect(resolveRenderer("tour.slides.md").id).toBe("deck");
    expect(resolveRenderer("tour.slides").id).toBe("deck");
    expect(resolveRenderer("notes.md").id).toBe("generic-md");
  });

  it("keeps a preference ahead of a longer registry extension", async () => {
    const manifests: Manifest[] = [
      { id: "generic-md", name: "Generic MD", extensions: [".md"], embed: "iframe" },
      { id: "deck", name: "Deck", extensions: [".slides.md"], embed: "iframe" },
    ];
    const { resolveRenderer } = await loadModule(manifests, { ".md": "generic-md" });
    expect(resolveRenderer("tour.slides.md").id).toBe("generic-md");
  });

  it("prefers a per-extension preference (matched by renderer id) over the registry", async () => {
    const { resolveRenderer } = await loadModule(MANIFESTS, { ".ts": "schema_form" });
    expect(resolveRenderer("main.ts").id).toBe("schema_form");
  });

  it("ignores a preference that names a renderer by display name (id-only)", async () => {
    // No back-compat: a pref value must be a renderer id, not a display name.
    const { resolveRenderer } = await loadModule(MANIFESTS, { ".ts": "Schema Form" });
    expect(resolveRenderer("main.ts").id).toBe("code-pro"); // falls back to the ext match
  });

  it("uses the wildcard preference when nothing else matches", async () => {
    const { resolveRenderer } = await loadModule(MANIFESTS, { "*": "code-pro" });
    expect(resolveRenderer("strange.xyz").id).toBe("code-pro");
  });

  it("routes images and binaries before any preference", async () => {
    const { resolveRenderer } = await loadModule(MANIFESTS, { "*": "code-pro", ".png": "code-pro" });
    expect(resolveRenderer("shot.PNG").id).toBe("pictures");
    expect(resolveRenderer("shot.png").name).toBe("Image Viewer");
    expect(resolveRenderer("bundle.zip").id).toBe("binary");
    // A PDF is binary with a renderer of its own: the one that is the default
    // for it, never a preference, and the binary view only until one is.
    expect(resolveRenderer("paper.PDF").id).toBe("papers");
  });

  it("a PDF opens in the binary view until a renderer is the default for it", async () => {
    const empty = await loadModule([], { "*": "code-pro" });
    expect(empty.resolveRenderer("paper.pdf").id).toBe("binary");
    // Claiming the extension is not enough: a binary file has no text to show.
    const claimed = await loadModule([{ id: "papers", name: "PDF", extensions: [".pdf"], embed: "native" }, TEXT], {});
    expect(claimed.resolveRenderer("paper.pdf").id).toBe("binary");
    expect(claimed.rendererCandidatesFor("paper.pdf").map((r) => r.id)).toEqual(["binary", "papers"]);
  });

  it("an unclaimed text file opens in the built-in text renderer, an image in the binary view without an image viewer", async () => {
    const { resolveRenderer } = await loadModule([TEXT], {});
    for (const name of ["notes.md", "sketch.excalidraw", "main.py", "anything.xyz"]) {
      expect(resolveRenderer(name)).toMatchObject({ id: "text", contentHash: "t1" });
    }
    expect(resolveRenderer("photo.jpeg").id).toBe("binary");
    expect(resolveRenderer("archive.zip").id).toBe("binary");
    // An image viewer takes the images it is the default for, whatever its id.
    const withImage = await loadModule([
      { id: "pictures", name: "Image Viewer", extensions: [".png", ".jpeg"], default_for: [".jpeg"], embed: "native" }, TEXT], {});
    expect(withImage.resolveRenderer("photo.jpeg").id).toBe("pictures");
    expect(withImage.resolveRenderer("photo.png").id).toBe("binary");
  });

  it("a renderer that is the default for an extension wins its tie, whatever the registry order", async () => {
    const manifests: Manifest[] = [
      { id: "first-md", name: "First MD", extensions: [".md"], embed: "iframe" },
      { id: "chosen-md", name: "Chosen MD", extensions: [".md", ".MDX"], default_for: [".md", ".mdx"], embed: "iframe" },
      { id: "deck", name: "Deck", extensions: [".slides.md"], embed: "iframe" },
      { id: "svg-editor", name: "SVG Source", extensions: [".svg"], embed: "iframe" },
      { id: "pictures", name: "Pictures", extensions: [".svg"], default_for: [".svg"], embed: "native" },
      TEXT,
    ];
    const mod = await loadModule(manifests, {});
    expect(mod.resolveRenderer("notes.md").id).toBe("chosen-md");
    expect(mod.resolveRenderer("notes.mdx").id).toBe("chosen-md");
    // A longer claim still wins over a default for a shorter one.
    expect(mod.resolveRenderer("tour.slides.md").id).toBe("deck");
    // An image another renderer claims as text opens as an image.
    expect(mod.resolveRenderer("logo.svg").id).toBe("pictures");
    expect(mod.rendererCandidatesFor("notes.md").map((r) => r.id)).toEqual(["chosen-md", "first-md", "text"]);
    // A preference still wins over a default.
    const preferred = await loadModule(manifests, { ".md": "first-md" });
    expect(preferred.resolveRenderer("notes.md").id).toBe("first-md");
  });

  it("keeps the previous cache when a refresh fails", async () => {
    const mod = await loadModule(MANIFESTS, {});
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("net down"); }));
    await mod.refreshRendererCache();
    expect(mod.resolveRenderer("settings.set_yaml").id).toBe("schema_form");
    err.mockRestore();
  });

  it("keeps the previous preferences when the prefs endpoint returns an error", async () => {
    const mod = await loadModule(MANIFESTS, { ".ts": "schema_form" });
    vi.stubGlobal("fetch", vi.fn(async (url: unknown) =>
      String(url).includes("renderer_preferences")
        ? { ok: false, status: 500, json: async () => ({ detail: "boom" }) }
        : { ok: true, json: async () => MANIFESTS },
    ));
    await mod.refreshRendererCache();
    expect(mod.resolveRenderer("main.ts").id).toBe("schema_form");
  });

  it("runs another pass for a refresh requested while one is in flight", async () => {
    const mod = await loadModule(MANIFESTS, {});
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    vi.stubGlobal("fetch", vi.fn(async (url: unknown) => {
      await gate;
      return { ok: true, json: async () => (String(url).includes("renderer_preferences") ? {} : MANIFESTS) };
    }));
    const first = mod.refreshRendererCache();
    // The in-flight pass has already issued its requests; new prefs land now.
    stubFetch(MANIFESTS, { ".ts": "schema_form" });
    const second = mod.refreshRendererCache();
    release();
    await first;
    await second;
    expect(mod.resolveRenderer("main.ts").id).toBe("schema_form");
  });
});

describe("useRendererId", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it('withholds the initial fallback until catalog, preferences and aliases resolve', async () => {
    vi.resetModules();
    let release!: () => void;
    const catalog = new Promise<void>(resolve => { release = resolve; });
    vi.stubGlobal('fetch', vi.fn(async (url: unknown) => {
      if (String(url).endsWith('/renderers')) await catalog;
      return {ok:true,json:async()=>String(url).endsWith('/renderers')
        ? [{id:'text',extensions:['.txt'],embed:'native',content_hash:'authoritative'}]
        : String(url).endsWith('/handlers') ? [] : {}};
    }));
    const mod = await import('./useRendererId');
    const {result} = renderHook(() => mod.useRendererId('notes.txt'));
    expect(result.current).toEqual({renderer:null,error:null});
    await act(async()=>{release();await mod.refreshRendererCache();});
    expect(result.current.renderer).toMatchObject({id:'text',contentHash:'authoritative'});
    const initial=result.current;
    await act(async()=>{await mod.refreshRendererCache();});
    expect(result.current).toBe(initial);
  });

  it('shows initial resolution failure and admits a renderer only after explicit retry succeeds', async () => {
    vi.resetModules();
    const log=vi.spyOn(console,'error').mockImplementation(()=>{});
    vi.stubGlobal('fetch',vi.fn(async()=>({ok:false,status:503})));
    const mod=await import('./useRendererId');
    const {result}=renderHook(()=>mod.useRendererId('notes.txt'));
    await waitFor(()=>expect(result.current.error).toContain('HTTP 503'));
    expect(result.current.renderer).toBeNull();
    stubFetch([{id:'text',extensions:['.txt'],embed:'native',content_hash:'ready'}],{});
    await act(async()=>{await mod.refreshRendererCache();});
    expect(result.current.error).toBeNull();
    expect(result.current.renderer?.contentHash).toBe('ready');
    log.mockRestore();
  });

  it("resolves on mount and re-resolves when the registry changes", async () => {
    const mod = await loadModule([TEXT], {});
    const { result } = renderHook(() => mod.useRendererId("page.set_yaml"));
    expect(result.current.renderer?.id).toBe("text"); // unclaimed: the built-in text
    stubFetch(MANIFESTS, {});
    await mod.refreshRendererCache();
    await waitFor(() => expect(result.current.renderer?.id).toBe("schema_form"));
  });

  it("honors an 'Open with' override and falls back when it vanishes", async () => {
    const mod = await loadModule(MANIFESTS, {});
    const { result } = renderHook(() => mod.useRendererId("main.ts", "schema_form"));
    expect(result.current.renderer?.id).toBe("schema_form");
    const { result: gone } = renderHook(() => mod.useRendererId("main.ts", "deleted-renderer"));
    expect(gone.current.renderer?.id).toBe("code-pro"); // primary resolution
  });

  it("resolves a new name in the render that brings it: one commit per keystroke, no effect-driven second one", async () => {
    const mod = await loadModule(MANIFESTS, {});
    let commits = 0;
    const { result, rerender } = renderHook(({ name }) => {
      useEffect(() => { commits++; });
      return mod.useRendererId(name);
    }, { initialProps: { name: "notes.t" } });
    expect(result.current.renderer?.id).toBe("text");
    const before = commits;
    rerender({ name: "notes.ts" });
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(result.current.renderer?.id).toBe("code-pro");
    expect(commits - before).toBe(1);
  });

  it("keeps the same record across mount and unrelated registry refreshes", async () => {
    const mod = await loadModule(MANIFESTS, {});
    // An override lookup builds a fresh (equal) record on every resolution.
    const { result } = renderHook(() => mod.useRendererId("main.ts", "schema_form"));
    const first = result.current;
    stubFetch([...MANIFESTS, { id: "other", name: "Other", extensions: [".other"], embed: "iframe" }], {});
    await act(async () => { await mod.refreshRendererCache(); });
    expect(mod.resolveRenderer("x.other").id).toBe("other");
    expect(result.current).toBe(first);
  });
});

describe("rendererCandidatesFor", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("lists the primary first, then registry matches, then the defaults for any text file", async () => {
    const mod = await loadModule(
      [...USER_MANIFESTS, { id: "ts-alt", name: "TS Alt", extensions: [".ts"], embed: "iframe" },
        { id: "code-editor", name: "Code Editor", extensions: [".py"], default_for: ["*"], embed: "iframe" }, TEXT],
      {},
    );
    const ids = mod.rendererCandidatesFor("main.ts").map((r) => r.id);
    expect(ids).toEqual(["code-pro", "ts-alt", "code-editor", "text"]);
    // Every text file, claimed or not, offers both; a binary file neither.
    expect(mod.rendererCandidatesFor("strange.xyz").map((r) => r.id)).toEqual(["text", "code-editor"]);
    expect(mod.rendererCandidatesFor("shot.png").map((r) => r.id)).toEqual(["pictures"]);
  });

  it("always offers plain text as a last resort for arbitrary text files", async () => {
    const mod = await loadModule(MANIFESTS, {});
    const ids = mod.rendererCandidatesFor("strange.xyz").map((r) => r.id);
    expect(ids[ids.length - 1]).toBe("text");
    // A file whose PRIMARY resolution is already text doesn't get a dupe.
    const txtIds = mod.rendererCandidatesFor("notes.txt").map((r) => r.id);
    expect(txtIds.filter((id) => id === "text")).toHaveLength(1);
    // Only a renderer that is a default for any text file is offered for one
    // it does not claim.
    expect(ids).toEqual(["text"]);
  });

  it("withholds text editors from image/binary files", async () => {
    const mod = await loadModule(MANIFESTS, {});
    expect(mod.rendererCandidatesFor("shot.png").map((r) => r.id)).toEqual(["pictures"]);
    expect(mod.rendererCandidatesFor("doc.pdf").map((r) => r.id)).toEqual(["papers"]);
  });

  it("allRenderers lists every registry renderer by name then id, never the app's own definition or binary views", async () => {
    const mod = await loadModule(
      [
        { id: "zeta-view", name: "Zeta View", extensions: [".zv"], embed: "native" },
        { id: "b-twin", name: "Twin" },
        { id: "a-twin", name: "Twin" },
        { id: "mono-view", name: "Mono View", extensions: [".mv"] },
      ],
      {},
    );
    expect(mod.allRenderers().map((r) => r.id)).toEqual(["mono-view", "a-twin", "b-twin", "zeta-view"]);
    expect(mod.allRenderers().find((r) => r.id === "zeta-view")).toEqual(mod.resolveRenderer("notes.zv"));
    // A definition file's candidates carry its app-owned view; the registry list does not.
    expect(mod.rendererCandidatesFor("pkg/view.renderer")[0].id).toBe("definition-renderer");
    expect(mod.allRenderers().some((r) => r.id.startsWith("definition-") || r.id === "binary")).toBe(false);
  });

  it("resolveRendererWithOverride takes a registered renderer only; an absent one falls back to the primary", async () => {
    const mod = await loadModule(MANIFESTS, {});
    expect(mod.resolveRendererWithOverride("settings.set_yaml", "text").id).toBe("text");
    // 'absent-editor' isn't in this channel's registry.
    expect(mod.resolveRendererWithOverride("settings.set_yaml", "absent-editor").id).toBe("schema_form");
    expect(mod.resolveRendererWithOverride("settings.set_yaml").id).toBe("schema_form");
  });
});

describe("supported extensions", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("lists every registry extension with its renderers, sorted", async () => {
    const mod = await loadModule(
      [
        ...MANIFESTS,
        { id: "yaml-viewer", name: "YAML Viewer", extensions: [".SET_YAML"], embed: "iframe" },
      ],
      {},
    );
    const exts = mod.getSupportedExtensions();
    expect(exts.map((e) => e.ext)).toEqual([...exts.map((e) => e.ext)].sort((a, b) => a.localeCompare(b)));
    expect(exts.find((e) => e.ext === ".set_yaml")?.renderers).toEqual(["Schema Form", "YAML Viewer"]);
    expect(exts.find((e) => e.ext === ".png")?.renderers).toEqual(["Image Viewer"]);
    // Image types no registered renderer claims are not offered.
    expect(exts.find((e) => e.ext === ".jpg")).toBeUndefined();
  });

  it("useSupportedExtensions refreshes when the renderer registry changes", async () => {
    const mod = await loadModule([], {});
    const { result } = renderHook(() => mod.useSupportedExtensions());
    expect(result.current.some((e) => e.ext === ".set_yaml")).toBe(false);
    stubFetch(MANIFESTS, {});
    await mod.refreshRendererCache();
    await waitFor(() => expect(result.current.some((e) => e.ext === ".set_yaml")).toBe(true));
  });
});

/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, beforeEach, vi } from "vitest";

// getApiUrl reads window.location + localStorage + import.meta.env. Each
// test shapes those inputs and re-imports the module so the result is
// exercised fresh.

async function importWith({
  port,
  stored,
  remote,
}: {
  port: string;
  stored: string | null;
  remote?: string;
}) {
  localStorage.clear();
  if (stored !== null) localStorage.setItem("apiUrl", stored);
  vi.stubGlobal("location", {
    ...window.location,
    port,
  });
  vi.stubEnv("VITE_REMOTE_API_URL", remote ?? "");
  vi.resetModules();
  const mod = await import("../api");
  return mod.getApiUrl;
}

describe("getApiUrl", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    localStorage.clear();
  });

  it("returns ../api when served from a non-dev port and no override", async () => {
    const getApiUrl = await importWith({ port: "4321", stored: null });
    expect(getApiUrl()).toBe("../api");
  });

  it("returns VITE_REMOTE_API_URL when served from vite dev port", async () => {
    const getApiUrl = await importWith({
      port: "5173",
      stored: null,
      remote: "http://remote.example/api",
    });
    expect(getApiUrl()).toBe("http://remote.example/api");
  });

  it("falls back to localhost:5001 on dev port when no env override", async () => {
    const getApiUrl = await importWith({ port: "5173", stored: null });
    expect(getApiUrl()).toBe("http://127.0.0.1:5001/api");
  });

  it("user-stored custom URL is respected", async () => {
    const getApiUrl = await importWith({
      port: "4321",
      stored: "https://api.custom/v1",
    });
    expect(getApiUrl()).toBe("https://api.custom/v1");
  });
});

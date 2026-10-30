import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiSettingsModal } from "./ApiSettingsModal";

describe("ApiSettingsModal", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("returns null when closed", () => {
    const { container } = render(
      <ApiSettingsModal isOpen={false} onClose={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("does not persist the resolved URL on mount", () => {
    render(<ApiSettingsModal isOpen={false} onClose={() => {}} />);
    expect(localStorage.getItem("apiUrl")).toBeNull();
  });

  it("clearing the URL input removes the persisted value", async () => {
    const user = userEvent.setup();
    localStorage.setItem("apiUrl", "https://seeded.example/api");

    render(<ApiSettingsModal isOpen={true} onClose={() => {}} />);
    await user.clear(screen.getByLabelText("Backend URL"));
    expect(localStorage.getItem("apiUrl")).toBeNull();
  });

  it("seeds the URL input from the persisted value and writes back on edit", async () => {
    const user = userEvent.setup();
    localStorage.setItem("apiUrl", "https://seeded.example/api");

    render(<ApiSettingsModal isOpen={true} onClose={() => {}} />);
    const input = screen.getByLabelText("Backend URL") as HTMLInputElement;
    expect(input.value).toBe("https://seeded.example/api");

    await user.clear(input);
    await user.type(input, "https://new.example/api");
    expect(localStorage.getItem("apiUrl")).toBe("https://new.example/api");
  });

  it("Test Connection renders JSON response on success", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ status: "ok" }),
    });
    vi.stubGlobal("fetch", fetchSpy);

    render(<ApiSettingsModal isOpen={true} onClose={() => {}} />);
    await user.click(screen.getByRole("button", { name: /test connection/i }));
    expect(fetchSpy).toHaveBeenCalled();
    const pre = await screen.findByText(/"status": "ok"/);
    expect(pre).toBeInTheDocument();
  });

  it("Test Connection surfaces non-OK status in the response area", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => ({}) }),
    );

    render(<ApiSettingsModal isOpen={true} onClose={() => {}} />);
    await user.click(screen.getByRole("button", { name: /test connection/i }));
    const pre = await screen.findByText(/Error fetching from API/);
    expect(pre).toBeInTheDocument();
    expect(pre.textContent).toMatch(/503/);
  });

  it("Escape fires onClose (no focus trap)", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<ApiSettingsModal isOpen={true} onClose={onClose} />);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("native label association focuses the editable backend input __F0955", async () => {
    const user = userEvent.setup();
    render(<ApiSettingsModal isOpen={true} onClose={() => {}} />);
    await user.click(screen.getByText("Backend URL"));
    expect(screen.getByLabelText("Backend URL")).toHaveFocus();
  });

  it("paints the field, action and response from the skin tokens (S04)", () => {
    render(<ApiSettingsModal isOpen={true} onClose={() => {}} />);
    expect(screen.getByLabelText("Backend URL")).toHaveClass("bg-sheet", "border-line", "font-mono");
    expect(screen.getByRole("button", { name: "Test Connection" })).toHaveClass("bg-sheet", "text-ink", "w-full");
    const response = screen.getByText("No response yet.");
    expect(response).toHaveClass("bg-code-bg", "border-line", "text-ink-2");
    expect(response.className).not.toMatch(/slate-|bg-white|dark:/);
  });
});

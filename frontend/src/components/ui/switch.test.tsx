import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Switch } from "./switch";

describe("Switch", () => {
  it("toggles checked state when clicked", async () => {
    const user = userEvent.setup();
    const onCheckedChange = vi.fn();
    render(<Switch onCheckedChange={onCheckedChange} />);
    const sw = screen.getByRole("switch");
    expect(sw).toHaveAttribute("data-state", "unchecked");
    await user.click(sw);
    expect(onCheckedChange).toHaveBeenCalledWith(true);
  });

  it("controlled: reflects the checked prop", () => {
    const { rerender } = render(<Switch checked={false} onCheckedChange={() => {}} />);
    expect(screen.getByRole("switch")).toHaveAttribute("data-state", "unchecked");
    rerender(<Switch checked={true} onCheckedChange={() => {}} />);
    expect(screen.getByRole("switch")).toHaveAttribute("data-state", "checked");
  });

  it("does not fire when disabled", async () => {
    const user = userEvent.setup();
    const onCheckedChange = vi.fn();
    render(<Switch disabled onCheckedChange={onCheckedChange} />);
    await user.click(screen.getByRole("switch"));
    expect(onCheckedChange).not.toHaveBeenCalled();
  });
});

it("is the mock's toggle: ink when on, line-2 when off, a sheet knob (S04)", () => {
  render(<Switch checked onCheckedChange={() => {}} />);
  const root = screen.getByRole("switch");
  expect(root.className).toContain("data-[state=checked]:bg-ink");
  expect(root.className).toContain("data-[state=unchecked]:bg-line-2");
  expect(root.className).not.toMatch(/bg-primary|bg-input|ring-ring/);
  const thumb = root.querySelector('[data-slot="switch-thumb"]')!;
  expect(thumb.className).toContain("bg-sheet");
  expect(thumb.className).not.toMatch(/bg-background|bg-foreground/);
});

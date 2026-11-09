import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Input } from "./input";

describe("Input", () => {
  it("forwards value and fires onChange", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Input placeholder="name" onChange={onChange} />);
    const el = screen.getByPlaceholderText("name");
    await user.type(el, "abc");
    expect(onChange).toHaveBeenCalled();
    expect((el as HTMLInputElement).value).toBe("abc");
  });

  it("honors disabled", async () => {
    const user = userEvent.setup();
    render(<Input placeholder="x" disabled />);
    const el = screen.getByPlaceholderText("x") as HTMLInputElement;
    expect(el).toBeDisabled();
    await user.type(el, "nope");
    expect(el.value).toBe("");
  });

  it("forwards ref to the underlying input", () => {
    const ref: { current: HTMLInputElement | null } = { current: null };
    render(<Input ref={ref} data-testid="i" />);
    expect(ref.current).toBe(screen.getByTestId("i"));
  });
});

it("is the shared field on the skin tokens and a caller's class still wins (S04)", () => {
  render(<><Input placeholder="field" /><Input placeholder="tight" className="h-6 px-1" /></>);
  const field = screen.getByPlaceholderText("field").className;
  expect(field).toContain("border-line");
  expect(field).toContain("bg-sheet");
  expect(field).toContain("placeholder:text-ink-3");
  expect(field).toContain("focus-visible:ring-1");
  expect(field).toContain("focus-visible:ring-teal/50");
  expect(field).not.toMatch(/border-input|bg-background|text-muted-foreground|ring-ring/);
  const tight = screen.getByPlaceholderText("tight").className;
  expect(tight).toContain("h-6");
  expect(tight).not.toContain("h-[30px]");
});

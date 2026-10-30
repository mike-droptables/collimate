import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Button } from "./button";

describe("Button", () => {
  it("renders its children", () => {
    render(<Button>Click me</Button>);
    expect(screen.getByRole("button", { name: "Click me" })).toBeInTheDocument();
  });

  it("fires onClick", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Go</Button>);
    await user.click(screen.getByRole("button", { name: "Go" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("respects disabled state", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Nope
      </Button>,
    );
    const btn = screen.getByRole("button", { name: "Nope" });
    expect(btn).toBeDisabled();
    await user.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });
});

describe("Button skin (S04)", () => {
  it("each variant paints on the skin tokens, never the retired shadcn theme utilities (S04)", () => {
    render(
      <>
        <Button>Primary</Button>
        <Button variant="destructive">Delete</Button>
        <Button variant="outline">Outline</Button>
        <Button variant="ghost">Ghost</Button>
      </>,
    );
    const cls = (name: string) => screen.getByRole("button", { name }).className;
    expect(cls("Primary")).toContain("bg-sheet");
    expect(cls("Primary")).toContain("[&_svg]:text-brass");
    expect(cls("Delete")).toContain("text-red");
    expect(cls("Delete")).toContain("hover:bg-red-soft");
    expect(cls("Outline")).toContain("border-line-2");
    expect(cls("Ghost")).toContain("border-transparent");
    expect(cls("Ghost")).not.toContain("border-line-2");
    for (const name of ["Primary", "Delete", "Outline", "Ghost"]) {
      expect(cls(name)).not.toMatch(/\b(?:bg|text|border|ring)-(?:primary|destructive|background|accent|input|ring)\b|text-white/);
    }
  });

  it("sizes follow the mock's bars (S04)", () => {
    render(<><Button>Bar</Button><Button size="sm">Run</Button><Button size="icon-sm" aria-label="Tool" /></>);
    expect(screen.getByRole("button", { name: "Bar" }).className).toContain("h-[1.625rem]");
    expect(screen.getByRole("button", { name: "Run" }).className).toContain("h-6");
    expect(screen.getByRole("button", { name: "Run" }).className).not.toContain("h-[1.625rem]");
    expect(screen.getByRole("button", { name: "Tool" }).className).toContain("size-[1.375rem]");
  });
});

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal } from "./modal";

describe("Modal", () => {
  afterEach(() => {
    document.body.style.overflow = "";
  });

  it("renders nothing when isOpen=false", () => {
    const { container } = render(
      <Modal isOpen={false} onClose={() => {}} title="T">
        <div>hidden</div>
      </Modal>,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders title + children with a dialog role when open", () => {
    render(
      <Modal isOpen={true} onClose={() => {}} title="Settings">
        <div data-testid="body">hello</div>
      </Modal>,
    );
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByTestId("body")).toBeInTheDocument();
  });

  it("close button fires onClose", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Modal isOpen={true} onClose={onClose} title="X">
        body
      </Modal>,
    );
    // Close button is the icon Button inside the header.
    const closeBtn = screen.getAllByRole("button")[0];
    await user.click(closeBtn);
    expect(onClose).toHaveBeenCalled();
  });

  it("Escape fires onClose", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Modal isOpen={true} onClose={onClose} title="X">
        body
      </Modal>,
    );
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("locks body scroll while open and restores on close", () => {
    const { rerender } = render(
      <Modal isOpen={true} onClose={() => {}} title="X">
        body
      </Modal>,
    );
    expect(document.body.style.overflow).toBe("hidden");
    rerender(
      <Modal isOpen={false} onClose={() => {}} title="X">
        body
      </Modal>,
    );
    expect(document.body.style.overflow).toBe("unset");
  });
});

it("sits on the lift scrim as a solid sheet plate, never white or slate (S04)", () => {
  render(
    <Modal isOpen={true} onClose={() => {}} title="Skin">
      body
    </Modal>,
  );
  const plate = screen.getByRole("dialog");
  expect(plate.className).toContain("bg-sheet");
  expect(plate.className).toContain("shadow-(--shadow)");
  expect(plate.parentElement!.className).toContain("bg-(--lift)");
  for (const el of [plate, plate.parentElement!, plate.firstElementChild!]) {
    expect(el.className).not.toMatch(/bg-white|bg-black|slate-/);
  }
  expect(plate.firstElementChild!.className).toContain("border-line");
});

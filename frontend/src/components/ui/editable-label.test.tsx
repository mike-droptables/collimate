import { describe, it, expect, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EditableLabel } from "./editable-label";
import { KEYBOARD_OWNER_ATTR, eventPathHasAttr } from "@/lib/keyboardOwner";
import { RETIRED_TOKENS } from '@/test/retiredTokens';

describe("EditableLabel", () => {
  it("renders the display value in view mode", () => {
    render(<EditableLabel value="Hello" onSave={() => {}} />);
    expect(screen.getByText("Hello")).toBeInTheDocument();
  });

  it("clicking swaps to edit mode and focuses the input", async () => {
    const user = userEvent.setup();
    render(<EditableLabel value="old" onSave={() => {}} />);
    await user.click(screen.getByText("old"));
    const input = screen.getByRole("textbox") as HTMLInputElement;
    expect(input).toHaveFocus();
    expect(input.value).toBe("old");
  });

  it("Enter commits trimmed value via onSave", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<EditableLabel value="old" onSave={onSave} />);
    await user.click(screen.getByText("old"));
    const input = screen.getByRole("textbox");
    await user.clear(input);
    await user.type(input, "  new  {Enter}");
    expect(onSave).toHaveBeenCalledExactlyOnceWith("new");
    // Returns to view mode after save
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("Escape discards changes without firing onSave", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<EditableLabel value="old" onSave={onSave} />);
    await user.click(screen.getByText("old"));
    const input = screen.getByRole("textbox");
    await user.clear(input);
    await user.type(input, "scratch{Escape}");
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText("old")).toBeInTheDocument();
  });

  it.each([false, true])("Escape stays with the editor (multiline=%s): the scene handler skips it and it does not bubble to window", async (multiline) => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<EditableLabel value="keep" onSave={onSave} multiline={multiline} />);
    await user.click(screen.getByText("keep"));
    const seen = vi.fn();
    const bubbled = vi.fn();
    const capture = (e: Event) => seen(eventPathHasAttr(e, KEYBOARD_OWNER_ATTR));
    window.addEventListener("keydown", capture, true);
    window.addEventListener("keydown", bubbled);
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Escape" });
    window.removeEventListener("keydown", capture, true);
    window.removeEventListener("keydown", bubbled);
    expect(seen).toHaveBeenCalledWith(true);
    expect(bubbled).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("blur without change does not fire onSave", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<EditableLabel value="same" onSave={onSave} />);
    await user.click(screen.getByText("same"));
    await user.tab();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("falls back to placeholder when value is empty", () => {
    render(<EditableLabel value="" onSave={() => {}} placeholder="unnamed" />);
    expect(screen.getByText("unnamed")).toBeInTheDocument();
  });

  it("multiline: Enter inserts a newline (no save); blur commits the multi-line value", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<EditableLabel value="old" onSave={onSave} multiline />);
    await user.click(screen.getByText("old"));
    const ta = screen.getByRole("textbox");
    await user.clear(ta);
    await user.type(ta, "line1{Enter}line2");
    expect(onSave).not.toHaveBeenCalled(); // Enter did not commit
    await user.tab(); // blur commits
    expect(onSave).toHaveBeenCalledExactlyOnceWith("line1\nline2");
  });

  it("multiline: Escape reverts without saving", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<EditableLabel value="keep" onSave={onSave} multiline />);
    await user.click(screen.getByText("keep"));
    await user.type(screen.getByRole("textbox"), "scratch{Escape}");
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText("keep")).toBeInTheDocument();
  });
});

it('keeps an in-progress label draft when a server name arrives__F0963',()=>{
 const save=vi.fn();const view=render(<EditableLabel value="Original" onSave={save}/>);fireEvent.click(screen.getByText('Original'));fireEvent.change(screen.getByRole('textbox'),{target:{value:'My draft'}});view.rerender(<EditableLabel value="Server" onSave={save}/>);expect(screen.getByRole('textbox')).toHaveValue('My draft');fireEvent.keyDown(screen.getByRole('textbox'),{key:'Enter'});expect(save).toHaveBeenCalledExactlyOnceWith('My draft');
});

const nameInput = () => document.querySelector("input") as HTMLInputElement;

const altT = () =>
  document.dispatchEvent(
    new KeyboardEvent("keydown", { key: "t", altKey: true, bubbles: true, cancelable: true }),
  );

describe("EditableLabel", () => {
  it("renders the name in view mode and the placeholder when empty", () => {
    const { rerender } = render(
      <EditableLabel value="My Cell" onSave={() => {}} />,
    );
    expect(screen.getByText("My Cell")).toBeInTheDocument();
    rerender(
      <EditableLabel value="" onSave={() => {}} placeholder="unnamed" />,
    );
    expect(screen.getByText("unnamed")).toBeInTheDocument();
  });

  it("clicking the title enters edit mode with the name selected", async () => {
    const user = userEvent.setup();
    render(<EditableLabel value="old" onSave={() => {}} />);
    await user.click(screen.getByText("old"));
    expect(nameInput()).toHaveFocus();
    expect(nameInput().value).toBe("old");
  });

  it("Enter commits the trimmed name and exits edit mode", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<EditableLabel value="old" onSave={onSave} />);
    await user.click(screen.getByText("old"));
    await user.clear(nameInput());
    await user.type(nameInput(), "  new name  {Enter}");
    expect(onSave).toHaveBeenCalledExactlyOnceWith("new name");
    expect(document.querySelector("input")).toBeNull();
  });

  it("committing with nothing changed does not fire onSave", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<EditableLabel value="same" onSave={onSave} />);
    await user.click(screen.getByText("same"));
    await user.keyboard("{Enter}");
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText("same")).toBeInTheDocument();
  });

  it("Escape cancels the edit without saving", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<EditableLabel value="keep" onSave={onSave} />);
    await user.click(screen.getByText("keep"));
    await user.clear(nameInput());
    await user.type(nameInput(), "discarded{Escape}");
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText("keep")).toBeInTheDocument();
  });

  it("Escape stays with the editor: the scene handler skips it and it does not bubble to window", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<EditableLabel value="keep" onSave={onSave} />);
    await user.click(screen.getByText("keep"));
    const seen = vi.fn();
    const bubbled = vi.fn();
    const capture = (e: Event) => seen(eventPathHasAttr(e, KEYBOARD_OWNER_ATTR));
    window.addEventListener("keydown", capture, true);
    window.addEventListener("keydown", bubbled);
    fireEvent.keyDown(nameInput(), { key: "Escape" });
    window.removeEventListener("keydown", capture, true);
    window.removeEventListener("keydown", bubbled);
    expect(seen).toHaveBeenCalledWith(true);
    expect(bubbled).not.toHaveBeenCalled();
    expect(document.querySelector("input")).toBeNull();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("blur commits a changed name", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<EditableLabel value="old" onSave={onSave} />);
    await user.click(screen.getByText("old"));
    await user.clear(nameInput());
    await user.type(nameInput(), "renamed");
    nameInput().blur();
    expect(onSave).toHaveBeenCalledExactlyOnceWith("renamed");
  });

  it("is a NAME editor only: no chevron, no textarea, no hover panel", async () => {
    const user = userEvent.setup();
    render(<EditableLabel value="n" onSave={() => {}} />);
    // No expand chevron next to the name.
    expect(document.querySelector("svg")).toBeNull();
    // Hovering the title surfaces nothing extra.
    await user.hover(screen.getByText("n"));
    expect(document.querySelector("textarea")).toBeNull();
    // Edit mode is a single name input — still no textarea.
    await user.click(screen.getByText("n"));
    expect(document.querySelector("textarea")).toBeNull();
  });

  it("CELL_EDIT_TITLE toggles edit mode only when active", async () => {
    const onSave = vi.fn();
    const { unmount } = render(<EditableLabel value="n" onSave={onSave} isActive />);
    act(() => { altT(); });
    expect(nameInput()).toBeTruthy();
    act(() => { altT(); });
    expect(document.querySelector("input")).toBeNull();
    unmount();

    render(<EditableLabel value="m" onSave={() => {}} />);
    act(() => { altT(); });
    expect(document.querySelector("input")).toBeNull();
  });
});

it("hovers and edits on the skin tokens (S04)", async () => {
  const user = userEvent.setup();
  render(<EditableLabel value="name" onSave={() => {}} multiline />);
  const label = screen.getByText("name");
  expect(label.className).toContain("hover:bg-sheet-2");
  expect(label.className).toContain("hover:border-line");
  expect(label.className).not.toMatch(RETIRED_TOKENS);
  await user.click(label);
  const box = screen.getByRole("textbox");
  expect(box.className).toContain("border-line-2");
  expect(box.className).not.toMatch(RETIRED_TOKENS);
});

import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MarkdownView } from "./MarkdownView";

const follow = vi.hoisted(() => vi.fn(async () => {}));
vi.mock("@/lib/openLink", async (original) => ({ ...(await original<typeof import("@/lib/openLink")>()), openLink: follow }));

const CELL = { channel: "ch", cellId: "c1" };
import { retiredOr } from '@/test/retiredTokens';

describe("MarkdownView", () => {
  it("renders empty when content is empty", () => {
    const { container } = render(<MarkdownView cell={CELL} content="" />);
    // react-markdown still emits an outer wrapper; assert no paragraph.
    expect(container.querySelector("p")).toBeNull();
  });

  it("renders a heading", () => {
    render(<MarkdownView cell={CELL} content="# Title" />);
    expect(
      screen.getByRole("heading", { level: 1, name: "Title" }),
    ).toBeInTheDocument();
  });

  it("renders a paragraph with inline code", () => {
    const { container } = render(
      <MarkdownView cell={CELL} content="hello `world`" />,
    );
    expect(container.querySelector("code")).toHaveTextContent("world");
  });

  it("strikethrough ~~text~~ becomes a <del> element", () => {
    const { container } = render(<MarkdownView cell={CELL} content="plain ~~struck~~ end" />);
    const s = container.querySelector("del");
    expect(s).not.toBeNull();
    expect(s!).toHaveTextContent("struck");
  });

  it("html <br/> becomes a <br> element in output", () => {
    const { container } = render(
      <MarkdownView cell={CELL} content={"line one<br/>line two"} />,
    );
    expect(container.querySelector("br")).not.toBeNull();
    // The content should still include both lines.
    expect(container.textContent).toMatch(/line one.*line two/s);
  });

  it("applies the provided className to its root wrapper", () => {
    const { container } = render(
      <MarkdownView cell={CELL} content="x" className="test-class" />,
    );
    expect(
      container.querySelector(".test-class") ||
        (container.firstChild as HTMLElement)?.className.includes("test-class"),
    ).toBeTruthy();
  });

  it("does not spread react-markdown's node prop onto DOM elements", () => {
    const { container } = render(<MarkdownView cell={CELL} content={"# T\n\n**b** _i_"} />);
    expect(container.querySelector("[node]")).toBeNull();
  });

  it("keeps rendered elements across a re-render (stable component types)", () => {
    const { container, rerender } = render(
      <MarkdownView cell={CELL} content={"# H\n\n**bold**"} className="a" />,
    );
    const strong = container.querySelector("strong");
    const h1 = container.querySelector("h1");
    // A className change re-renders past the memo with the same content.
    rerender(<MarkdownView cell={CELL} content={"# H\n\n**bold**"} className="b" />);
    expect(container.querySelector("strong")).toBe(strong);
    expect(container.querySelector("h1")).toBe(h1);
  });

  it("renders GFM tables and nested deletion markup __F0790_F1862", () => {
    const { container } = render(
      <MarkdownView cell={CELL} content={"| Feature | Result |\n| --- | --- |\n| table | ~~**gone**~~ |"} />,
    );
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Feature" })).toBeInTheDocument();
    expect(container.querySelector("del strong")).toHaveTextContent("gone");
    expect(container.querySelector("[node]")).toBeNull();
  });

  it("keeps code literal and rejects executable HTML/links under GFM __F0790_F1862", () => {
    const { container } = render(
      <MarkdownView cell={CELL} content={"`~~literal~~`\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))"} />,
    );
    expect(container.querySelector("code")).toHaveTextContent("~~literal~~");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("a")?.getAttribute("href")).not.toMatch(/^javascript:/);
  });
});

describe("MarkdownView skin (S04)", () => {
  it("sets prose in the document face on the skin's inks, code on the code ground (S04)", () => {
    const { container } = render(
      <MarkdownView cell={CELL} content={"# Title\n\n## Section\n\ntext `inline`\n\n```\nblock\n```\n\n> quoted"} />,
    );
    const root = container.firstElementChild as HTMLElement;
    expect(root.className).toContain("font-prose");
    expect(root.className).toContain("text-prose-ink");
    expect(container.querySelector("h2")!.className).toContain("text-ink-2");
    expect(container.querySelector("p code")!.className).toContain("bg-code-bg");
    expect(container.querySelector("pre")!.className).toContain("bg-code-bg");
    expect(container.querySelector("blockquote")!.className).toContain("border-line-2");
    expect(container.innerHTML).not.toMatch(retiredOr('bg-white', '#050505'));
  });
});

describe("MarkdownView embed attribute block", () => {
    it('applies the block as the image style and never shows it as text', () => {
        const { container } = render(<MarkdownView cell={CELL} content={'![plot](plot.png){width=50% align=center}'} />);
        const img = container.querySelector('img')!;
        expect(img.getAttribute('alt')).toBe('plot');
        expect(img.style.width).toBe('50%');
        expect(img.style.display).toBe('block');
        expect(img.style.marginLeft).toBe('auto');
        expect(container.textContent).not.toContain('{');
    });

    it('keeps the text after the block and floats an inline image', () => {
        const { container } = render(<MarkdownView cell={CELL} content={'see ![a](a.png){width=20% align=right} beside'} />);
        const img = container.querySelector('img')!;
        expect(img.style.width).toBe('20%');
        expect(img.style.float).toBe('right');
        expect(container.querySelector('p')!.textContent).toBe('see  beside');
    });

    it('leaves braces that are not a valid block, or not right after an image, as text', () => {
        const { container } = render(<MarkdownView cell={CELL} content={'![a](a.png){foo} and ![b](b.png) {width=50%} and {width=50%}'} />);
        expect(container.textContent).toBe('{foo} and  {width=50%} and {width=50%}');
        for (const img of container.querySelectorAll('img')) expect(img.getAttribute('style')).toBeNull();
    });

    it('leaves a plain image and a link untouched', () => {
        const { container } = render(<MarkdownView cell={CELL} content={'![plot](plot.png)\n\n[plot](plot.png)'} />);
        expect(container.querySelector('img')!.getAttribute('style')).toBeNull();
        expect(container.querySelector('a')!.getAttribute('href')).toBe('plot.png');
    });

  it("every link follows through openLink with its cell and document; a relative image loads beside the document; absolute ones stay as they are", () => {
    follow.mockClear();
    render(<MarkdownView content={"[a](notes/a.md#x) [w](https://example.test) ![i](img/p.png) ![r](https://example.test/r.png)"} cell={{ ...CELL, documentPath: "docs/readme.md" }} />);
    const link = screen.getByRole("link", { name: "a" });
    expect(link).not.toHaveAttribute("target");
    expect(fireEvent.click(link)).toBe(false);
    expect(follow).toHaveBeenCalledWith("notes/a.md#x", { ...CELL, documentPath: "docs/readme.md" });
    expect(fireEvent.click(screen.getByRole("link", { name: "w" }))).toBe(false);
    expect(follow).toHaveBeenLastCalledWith("https://example.test", { ...CELL, documentPath: "docs/readme.md" });
    expect(screen.getByRole("img", { name: "i" }).getAttribute("src")).toMatch(/\/channels\/ch\/cells\/c1\/files\/docs\/img\/p\.png$/);
    expect(screen.getByRole("img", { name: "r" })).toHaveAttribute("src", "https://example.test/r.png");
  });

  it("without a document, relative paths resolve from the cell root; a #anchor stays in the view", () => {
    follow.mockClear();
    render(<MarkdownView content={"[a](notes/a.md) [top](#top) ![i](img/p.png)"} cell={CELL} />);
    fireEvent.click(screen.getByRole("link", { name: "a" }));
    expect(follow).toHaveBeenCalledWith("notes/a.md", CELL);
    expect(fireEvent.click(screen.getByRole("link", { name: "top" }))).toBe(false);
    expect(follow).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("img", { name: "i" }).getAttribute("src")).toMatch(/\/channels\/ch\/cells\/c1\/files\/img\/p\.png$/);
  });
});

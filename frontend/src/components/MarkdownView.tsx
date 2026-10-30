import React, { createContext, useContext, useMemo } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { cn } from '@/lib/utils';
import { remarkEmbedAttributes } from '@/lib/remarkEmbedAttributes';
import { cellPathOfHref } from '@/lib/cellLinks';
import { revealCellFile } from '@/lib/revealFile';
import { cellFileUrl } from '@/lib/api';

/** The cell a markdown text belongs to (an annotation message): its
 *  relative links open the cell's files, its relative images load them. */
export interface MarkdownCell {
    channel: string;
    cellId: string;
}

interface MarkdownViewProps {
    content: string;
    className?: string;
    cell?: MarkdownCell;
}

const MarkdownCellContext = createContext<MarkdownCell | null>(null);

const LINK_CLASS = "text-inherit underline decoration-line-2 decoration-1 underline-offset-[0.1875rem] hover:decoration-ink-2 transition-colors";

/** A link: a cell file opens in a new tab of its cell; anything else in a
 *  new browser tab. */
function MarkdownLink({ href, children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) {
    const cell = useContext(MarkdownCellContext);
    const path = cell ? cellPathOfHref(href) : null;
    if (cell && path) {
        return (
            <a
                href={href}
                title={path}
                onClick={(e) => { e.preventDefault(); revealCellFile(cell.cellId, path, { newTab: true }); }}
                className={LINK_CLASS}
                {...props}
            >
                {children}
            </a>
        );
    }
    return <a href={href} target="_blank" rel="noopener noreferrer" className={LINK_CLASS} {...props}>{children}</a>;
}

/** An image: a cell file's path loads from the cell. */
function MarkdownImage({ src, alt, ...props }: React.ImgHTMLAttributes<HTMLImageElement>) {
    const cell = useContext(MarkdownCellContext);
    const path = cell && typeof src === 'string' ? cellPathOfHref(src) : null;
    return (
        <img
            src={cell && path ? cellFileUrl(cell.channel, cell.cellId, path) : src}
            alt={alt}
            className="max-w-full h-auto rounded-md border border-line my-2"
            loading="lazy"
            {...props}
        />
    );
}

// Tokenize HTML tags that ReactMarkdown would otherwise strip: <br/> becomes
// a safe text sequence that Markdown considers plain text.
const BR_TOKEN = "§§BR§§";
// Only the safe line-break token needs custom text processing; Markdown's
// own parser handles deletion nodes, including nested inline formatting.
function processText(text: string): React.ReactNode[] {
    return text.split(BR_TOKEN).flatMap((part, i) => i ? [<br key={i} />, part] : [part]);
}

// Helper to recursively process children
function processChildren(children: React.ReactNode): React.ReactNode {
    return React.Children.map(children, (child) => {
        if (typeof child === 'string') {
            return processText(child);
        }
        return child;
    });
}

// Module-level so every render hands ReactMarkdown the same component types
// (a per-render map remounts the whole markdown DOM). Each override drops
// react-markdown's `node` prop so it is not spread onto the DOM element.
const MARKDOWN_COMPONENTS: Components = {
    // --- TYPOGRAPHY ---
    h1: ({ node: _node, children, ...props }) => (
        <h1 className="text-[1.55em] font-semibold tracking-[-0.01em] leading-tight text-prose-ink mt-4 mb-2" {...props}>
            {children}
        </h1>
    ),
    h2: ({ node: _node, children, ...props }) => (
        <h2 className="font-ui text-[0.84em] font-semibold uppercase tracking-[0.08em] text-ink-2 mt-[1.4em] mb-2" {...props}>
            {children}
        </h2>
    ),
    h3: ({ node: _node, children, ...props }) => (
        <h3 className="text-[1.03em] font-semibold leading-snug text-prose-ink mt-[1.15em] mb-1" {...props}>
            {children}
        </h3>
    ),
    h4: ({ node: _node, children, ...props }) => <h4 className="font-semibold text-prose-ink mt-2 mb-1" {...props}>{children}</h4>,
    h5: ({ node: _node, children, ...props }) => <h5 className="text-[0.9em] font-semibold text-prose-ink mt-2 mb-1" {...props}>{children}</h5>,
    h6: ({ node: _node, children, ...props }) => <h6 className="font-ui text-[0.78em] font-semibold uppercase tracking-[0.08em] text-ink-3 mt-2 mb-1" {...props}>{children}</h6>,

    // --- BLOCKS ---
    p: ({ node: _node, children, className, ...props }) => (
        <p className={cn("mb-2 last:mb-0 leading-relaxed min-h-[1.2em]", className)} {...props}>
            {processChildren(children)}
        </p>
    ),
    blockquote: ({ node: _node, children, ...props }) => (
        <blockquote className="border-l-2 border-line-2 pl-3.5 py-px my-2 text-ink-2" {...props}>
            {children}
        </blockquote>
    ),
    hr: ({ node: _node, ...props }) => <hr className="border-0 border-t border-line my-4" {...props} />,

    // --- LISTS ---
    ul: ({ node: _node, children, ...props }) => (
        <ul className="list-disc list-outside ml-4 mb-2 space-y-1 marker:text-ink-3" {...props}>
            {children}
        </ul>
    ),
    ol: ({ node: _node, children, ...props }) => (
        <ol className="list-decimal list-outside ml-4 mb-2 space-y-1 marker:text-ink-3" {...props}>
            {children}
        </ol>
    ),
    li: ({ node: _node, children, ...props }) => (
        <li className="pl-1" {...props}>
            {processChildren(children)}
        </li>
    ),

    // --- INLINE ---
    a: ({ node: _node, children, ...props }) => <MarkdownLink {...props}>{children}</MarkdownLink>,
    strong: ({ node: _node, children, ...props }) => <strong className="font-semibold" {...props}>{processChildren(children)}</strong>,
    em: ({ node: _node, children, ...props }) => <em className="italic" {...props}>{processChildren(children)}</em>,

    del: ({ node: _node, children, ...props }) => (
        <del className="text-ink-3 decoration-ink-3" {...props}>{processChildren(children)}</del>
    ),

    // --- CODE ---
    code: ({ node: _node, children, className, ...props }) => {
        const isBlock = /language-(\w+)/.test(className || '');
        if (isBlock) {
             return (
                <code className={cn("font-mono", className)} {...props}>
                    {children}
                </code>
             );
        }
        return (
            <code className="bg-code-bg font-mono text-[0.8em] px-1 py-px rounded-xs border border-line" {...props}>
                {children}
            </code>
        );
    },
    pre: ({ node: _node, children, ...props }) => (
        <pre className="bg-code-bg border border-line rounded-md px-3 py-2.5 overflow-x-auto my-2 font-mono text-[0.71875rem] leading-[1.55] text-ink-2 [&_code]:p-0 [&_code]:border-0 [&_code]:bg-transparent [&_code]:text-[length:inherit]" {...props}>
            {children}
        </pre>
    ),

    // --- TABLES ---
    table: ({ node: _node, children, ...props }) => (
        <div className="overflow-x-auto my-3">
            <table className="w-full border-collapse text-[0.92em]" {...props}>
                {children}
            </table>
        </div>
    ),
    thead: ({ node: _node, children, ...props }) => <thead {...props}>{children}</thead>,
    tbody: ({ node: _node, children, ...props }) => <tbody {...props}>{children}</tbody>,
    tr: ({ node: _node, children, ...props }) => <tr className="transition-colors hover:bg-sheet-2" {...props}>{children}</tr>,
    th: ({ node: _node, children, ...props }) => (
        <th className="border-b border-line-2 py-1.5 pr-3 text-left font-ui text-[0.71875rem] font-semibold uppercase tracking-[0.06em] text-ink-2" {...props}>
            {processChildren(children)}
        </th>
    ),
    td: ({ node: _node, children, ...props }) => (
        <td className="border-b border-line py-[0.4375rem] pr-3 align-top" {...props}>
            {processChildren(children)}
        </td>
    ),

    // --- MEDIA ---
    img: ({ node: _node, ...props }) => <MarkdownImage {...props} />,
};

// Module-level for the same reason as MARKDOWN_COMPONENTS.
const REMARK_PLUGINS = [remarkGfm, remarkEmbedAttributes];

/**
 * A wrapper around ReactMarkdown that:
 * 1. Sets the prose in the skin's document face and ink (the mock's `.md`).
 * 2. Parses GFM tables, strikethrough, task lists and autolinks.
 * 3. Adds support for HTML Line Breaks (<br/>) via Tokenization -> Component replacement.
 * 4. Applies the markdown editor's embed attribute block (`![a](b.png){width=50% align=center}`)
 *    as the image's style, so it never shows as text.
 * 5. With `cell`, opens relative links as that cell's files (a new tab) and
 *    loads relative images from it.
 */
// Memoized: a parent re-render with the same content skips the re-parse.
export const MarkdownView = React.memo(function MarkdownView({ content, className, cell }: MarkdownViewProps) {
    const processedContent = useMemo(() => {
        if (!content) return "";
        // Replace <br>, <br/>, <br /> (case insensitive) with the token
        return content.replace(/<br\s*\/?>/gi, BR_TOKEN);
    }, [content]);

    const channel = cell?.channel;
    const cellId = cell?.cellId;
    const cellValue = useMemo(() => (channel !== undefined && cellId !== undefined ? { channel, cellId } : null), [channel, cellId]);

    return (
        <div className={cn("font-prose text-prose-ink break-words", className)}>
            <MarkdownCellContext.Provider value={cellValue}>
                <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={MARKDOWN_COMPONENTS}>
                    {processedContent}
                </ReactMarkdown>
            </MarkdownCellContext.Provider>
        </div>
    );
});

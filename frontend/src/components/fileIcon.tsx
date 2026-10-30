import { File, FileCode, FileText, PenTool, Braces } from 'lucide-react';

/** A file's glyph: the shape tells the kind, the colour stays ink-3 (the
 *  mock's tree icons), so an icon never borrows an accent's meaning. */
const ICON = 'w-5 h-5 text-ink-3';

export const getFileIcon = (lang: string, name: string) => {
    if (name.endsWith('.excalidraw')) return <PenTool className={ICON} />;
    if (lang === 'typescript' || name.endsWith('.ts') || name.endsWith('.tsx')) return <FileCode className={ICON} />;
    if (lang === 'json') return <Braces className={ICON} />;
    if (lang === 'markdown' || name.endsWith('.md')) return <FileText className={ICON} />;
    return <File className={ICON} />;
};

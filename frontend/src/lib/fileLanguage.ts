// One extension -> language table for every frontend writer (create, rename,
// OS drop ingest). It matches the server's tables (routes/fs.py and friends).

const EXT_LANGUAGE: Record<string, string> = {
    'py': 'python', 'js': 'javascript', 'jsx': 'javascript',
    'ts': 'typescript', 'tsx': 'typescript', 'json': 'json',
    'html': 'html', 'css': 'css', 'scss': 'scss', 'less': 'less',
    'sh': 'shell', 'bash': 'shell', 'yaml': 'yaml', 'yml': 'yaml',
    'toml': 'toml', 'xml': 'xml', 'sql': 'sql', 'rs': 'rust',
    'go': 'go', 'java': 'java', 'c': 'c', 'cpp': 'cpp', 'cs': 'csharp',
    'rb': 'ruby', 'php': 'php', 'swift': 'swift', 'kt': 'kotlin',
    'md': 'markdown', 'markdown': 'markdown', 'txt': 'plaintext',
};

/** The language of a text file at `path`. Basename first: `dir.v2/README` has
 *  no extension, and neither has `Makefile` or `.env`; all are plaintext. */
export function languageForPath(path: string): string {
    const name = path.split('/').pop() ?? path;
    const dot = name.lastIndexOf('.');
    if (dot <= 0) return 'plaintext';
    return EXT_LANGUAGE[name.slice(dot + 1).toLowerCase()] ?? 'plaintext';
}

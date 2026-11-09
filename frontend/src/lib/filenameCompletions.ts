// Inline extension completion for a file name being typed: the small file
// creator (components/AddFileDialog) and the Files panel's New file here
// both offer it.

/** Match every dot boundary so a partially typed compound extension can
 *  complete as a whole. Completions only add characters to the typed name. */
export function filenameCompletions(name: string, extensions: readonly { ext: string }[]): string[] {
    const lower = name.toLowerCase();
    const completions = new Set<string>();
    for (const { ext } of extensions) {
        if (!ext.startsWith('.')) continue;
        for (let dot = name.indexOf('.'); dot !== -1; dot = name.indexOf('.', dot + 1)) {
            const prefix = lower.slice(dot);
            if (ext.toLowerCase().startsWith(prefix) && prefix.length < ext.length) {
                completions.add(name + ext.slice(prefix.length));
            }
        }
    }
    return [...completions];
}

// The slice of the File System Access API the transfer panel uses, typed
// structurally: TypeScript's DOM library leaves out the window pickers, and
// tests drive these shapes with plain fakes (jsdom has none of them).
// Chromium and the desktop app have them; Firefox and Safari do not.

export interface FsWritable extends WritableStream<Uint8Array> {
    close(): Promise<void>;
}

export interface FsFileHandle {
    kind: 'file';
    name: string;
    getFile(): Promise<File>;
    createWritable(): Promise<FsWritable>;
}

export interface FsDirectoryHandle {
    kind: 'directory';
    name: string;
    values(): AsyncIterable<FsFileHandle | FsDirectoryHandle>;
    getDirectoryHandle(name: string, options?: { create?: boolean }): Promise<FsDirectoryHandle>;
    getFileHandle(name: string, options?: { create?: boolean }): Promise<FsFileHandle>;
}

export interface FsPickerType {
    description?: string;
    accept: Record<string, string[]>;
}

export interface FsAccessWindow {
    showOpenFilePicker?: (options?: { multiple?: boolean; types?: FsPickerType[]; excludeAcceptAllOption?: boolean }) => Promise<FsFileHandle[]>;
    showDirectoryPicker?: (options?: { mode?: 'read' | 'readwrite' }) => Promise<FsDirectoryHandle>;
    showSaveFilePicker?: (options?: { suggestedName?: string; types?: FsPickerType[] }) => Promise<FsFileHandle>;
}

export const fsAccess = (): FsAccessWindow => (typeof window === 'undefined' ? {} : window as unknown as FsAccessWindow);

/** The user closed a picker without choosing: not a failure. */
export const isPickerCancel = (error: unknown): boolean => (error as { name?: string })?.name === 'AbortError';

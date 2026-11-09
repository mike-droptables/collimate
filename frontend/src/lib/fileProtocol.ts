/** Current file descriptors contain provenance, never mutable body bytes. */
export interface FileDescriptor {
    path: string;
    file_revision: number;
    body_hash: string | null;
    deleted: boolean;
    writer_id: string;
    edit_seq: number;
    language: string;
    encoding: 'utf-8' | 'binary';
}
export interface FileConflict {
    conflict_id: string; path: string; base_revision: number; current_revision: number;
    candidate_revision: number; candidate_hash: string | null; writer_id: string; edit_seq: number;
}
export interface FileMutation {
    path: string; writer_id: string; edit_seq: number; base_revision: number;
    kind: 'put' | 'delete'; content_text?: string; blob_hash?: string; language?: string; encoding?: 'utf-8' | 'binary';
}
/** A folder an operation renames, so the repositories at or under it
 *  re-key with its files. */
export interface FolderMove { source: string; target: string }
/** The folders an operation deletes or renames travel with it, so the host
 *  drops or re-keys the cell's repositories there in the same version. */
export interface FolderOperations { folder_deletes?: string[]; folder_moves?: FolderMove[] }
export interface FileOperation extends FolderOperations { operation_id: string; mutations: FileMutation[]; resolves: string[] }
export interface FileMutationResult {
    operation_id: string; outcome: 'accepted' | 'conflict' | 'superseded';
    files: FileDescriptor[]; conflicts: FileConflict[];
    /** The conflict ids an accepted operation resolved. */
    resolved?: string[];
}

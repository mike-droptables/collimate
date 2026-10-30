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
export interface FileOperation { operation_id: string; mutations: FileMutation[]; resolves: string[] }
export interface FileMutationResult {
    operation_id: string; outcome: 'accepted' | 'conflict' | 'superseded';
    files: FileDescriptor[]; conflicts: FileConflict[];
}

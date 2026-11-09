import type { StateCreator } from 'zustand';
import type { CollimateState } from '../useCollimateStore';
import { Cell, VirtualFile } from '@/lib/types';
import { replaceCellFiles } from '@/lib/fileCommands';
import { patchCellMeta } from '@/lib/api';
import { isRunPath, lockOwner } from '@/lib/runningCell';
import type { FileClipboard, FileDragState } from './types';


export interface FileTransferSlice {


  // Cross-cell file transfer. The clipboard survives channel switches, so
  // copy/cut in one channel can be pasted into a cell in another: it carries
  // accepted file descriptors whose pinned bodies the paste reads from the
  // source's channel and cell. A copy pastes that revision; a cut re-reads
  // its node from the source at paste time
  // (useFileTransfer). fileDrag is the in-flight drag payload —
  // dataTransfer can't be read during dragover, so the payload lives here and
  // only a marker MIME rides on the drag event. Both session-only.
  fileClipboard: FileClipboard | null;

  // The text the latest copy or cut put on the OS clipboard. A paste
  // clears fileClipboard after a cut but leaves this, so a later paste of
  // that same text is recognised as spent rather than offered as new text.
  copiedOsText: string | null;

  fileDrag: FileDragState | null;

  // An OS file drag is over the window (useCanvasFileDrop's window listeners
  // keep it). While it is, no renderer takes pointer events and every desktop
  // guest is occluded, so a renderer's own document never swallows the drag
  // the Files panes and the canvas are waiting for.
  osFileDragActive: boolean;

  // Cross-cell file transfer.
  setFileClipboard: (clip: FileClipboard) => void;

  clearFileClipboard: () => void;

  setFileDrag: (drag: FileDragState | null) => void;

  setOsFileDragActive: (active: boolean) => void;

  /** Find a cell by id across every channel's stacks (layout from
   *  state.channels, freshest content overlaid from cellCache). */
  locateCell: (cellId: string) => { channel: string; stackId: string; cellIndex: number; cell: Cell } | null;

  /** File operations use pinned bodies and conditional durable receipts. */
  applyCellFiles: (cellId: string, nextFiles: VirtualFile[]) => Promise<boolean>;
}

export const createFileTransferSlice: StateCreator<CollimateState, [], [], FileTransferSlice> = (set, get) => ({

    fileClipboard: null,

    copiedOsText: null,

    fileDrag: null,

    osFileDragActive: false,


    setFileClipboard: (clip) => set({ fileClipboard: clip, copiedOsText: clip.osText }),

    clearFileClipboard: () => set({ fileClipboard: null }),

    setFileDrag: (drag) => set({ fileDrag: drag }),

    setOsFileDragActive: (active) => {
      if (get().osFileDragActive !== active) set({ osFileDragActive: active });
    },


    locateCell: (cellId) => {
      const st = get().state;
      if (!st) return null;
      for (const ch of st.channels) {
        for (const stacks of [ch.main_stacks, ch.nav_stacks]) {
          for (const stack of stacks) {
            const idx = stack.cells.findIndex((c) => c.id === cellId);
            if (idx >= 0) {
              // Layout (channel/stack/position) is authoritative in
              // state.channels; the freshest file content lives in cellCache
              // (the SSE/optimistic merge keeps the hydrated copy). Overlay
              // the cache on top of the layout cell.
              const cached = get().cellCache[cellId];
              const cell = cached ? { ...stack.cells[idx], ...cached } : stack.cells[idx];
              return { channel: ch.id, stackId: stack.id, cellIndex: idx, cell };
            }
          }
        }
      }
      return null;
    },


    applyCellFiles: async (cellId,nextFiles) => {
      const location=get().locateCell(cellId);
      if(!location)return false;
      const {channel,cell}=location;
      const current=new Map(cell.files.map(file=>[file.name,file]));
      const locked=cell.files.filter(file=>isRunPath(file.name)||lockOwner(get(),cellId,file.name)!==null);
      const lockedNames=new Set(locked.map(file=>file.name));
      const desired=[...nextFiles.filter(file=>!lockedNames.has(file.name)&&(current.has(file.name)||!isRunPath(file.name))),...locked];
      try {
        await replaceCellFiles(channel,cellId,desired,cell.files);
        if(cell.pinned && !desired.some(file=>file.name===cell.pinned)){
          const receipt=await patchCellMeta(channel,cellId,{pinned:null});get().patchCell(cellId,receipt);
        }
        return true;
      }catch(error){console.error('applyCellFiles failed',cellId,error);return false;}
    },
});

import type { StateCreator } from 'zustand';
import type { CollimateState } from '../useCollimateStore';




/** Settle an open dialog with its cancelled value (choice -> null,
 *  confirm/alert -> false) so its awaiter doesn't hang when a new dialog
 *  replaces it. */
function settleOpenDialog(d: CollimateState['confirmDialog']): void {
  if (!d) return;
  if (d.kind === 'choice') d.resolve(null);
  else d.resolve(false);
}
export interface DialogSlice {


  // Confirm/alert dialog state. Replaces the native window.confirm /
  // window.alert modals so destructive prompts render in the app's styled
  // modal shell (mirrors ChannelDialog's look). `resolve` is the promise
  // settlement for the awaiter; kind='alert' dialogs show only a single OK
  // button (which still resolves `true` so callers can `await` uniformly).
  // A dialog about one cell's files names the cell (`cellId`): it shows as
  // a modal of that cell, over its body, not of the whole app.
  confirmDialog:
    | {
        kind: 'confirm' | 'alert';
        cellId?: string;
        title: string;
        body: string;
        confirmLabel: string;
        cancelLabel: string;
        destructive: boolean;
        resolve: (ok: boolean) => void;
      }
    | {
        // Multiple-choice variant: resolves the chosen option id, or null on
        // cancel (X / Esc / backdrop). No implicit primary — every path is an
        // explicit option button.
        kind: 'choice';
        cellId?: string;
        title: string;
        body: string;
        options: { id: string; label: string; destructive?: boolean }[];
        resolve: (id: string | null) => void;
      }
    | null;

  // Confirm / alert dialog.
  openConfirm: (opts: {
    title: string;
    body: string;
    confirmLabel?: string;
    cancelLabel?: string;
    destructive?: boolean;
    /** The cell the question is about: the dialog shows in that cell. */
    cellId?: string;
  }) => Promise<boolean>;

  openAlert: (opts: { title: string; body: string; okLabel?: string; cellId?: string }) => Promise<boolean>;

  /** Modal multiple-choice. Resolves the chosen option id, or null on cancel
   *  (X / Esc / backdrop). */
  openChoice: (opts: {
    title: string;
    body?: string;
    options: { id: string; label: string; destructive?: boolean }[];
  }) => Promise<string | null>;

  resolveConfirm: (ok: boolean) => void;

  resolveChoice: (id: string | null) => void;
}

export const createDialogSlice: StateCreator<CollimateState, [], [], DialogSlice> = (set, get) => ({

    confirmDialog: null,


    openConfirm: (opts) => {
      return new Promise<boolean>((resolve) => {
        settleOpenDialog(get().confirmDialog);
        set({
          confirmDialog: {
            kind: 'confirm',
            ...(opts.cellId ? { cellId: opts.cellId } : {}),
            title: opts.title,
            body: opts.body,
            confirmLabel: opts.confirmLabel ?? 'Confirm',
            cancelLabel: opts.cancelLabel ?? 'Cancel',
            destructive: opts.destructive ?? false,
            resolve,
          },
        });
      });
    },


    openAlert: (opts) => {
      return new Promise<boolean>((resolve) => {
        settleOpenDialog(get().confirmDialog);
        set({
          confirmDialog: {
            kind: 'alert',
            ...(opts.cellId ? { cellId: opts.cellId } : {}),
            title: opts.title,
            body: opts.body,
            confirmLabel: opts.okLabel ?? 'OK',
            cancelLabel: '',
            destructive: false,
            resolve,
          },
        });
      });
    },


    openChoice: (opts) => {
      return new Promise<string | null>((resolve) => {
        settleOpenDialog(get().confirmDialog);
        set({
          confirmDialog: {
            kind: 'choice',
            title: opts.title,
            body: opts.body ?? '',
            options: opts.options,
            resolve,
          },
        });
      });
    },


    resolveConfirm: (ok) => {
      const cur = get().confirmDialog;
      if (!cur || cur.kind === 'choice') return;
      cur.resolve(ok);
      set({ confirmDialog: null });
    },


    resolveChoice: (id) => {
      const cur = get().confirmDialog;
      if (!cur || cur.kind !== 'choice') return;
      cur.resolve(id);
      set({ confirmDialog: null });
    },
});

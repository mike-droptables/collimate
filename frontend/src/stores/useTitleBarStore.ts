import { create } from 'zustand';

// Whether the desktop shell's page-drawn title bar row is hidden.
// Toggled by clicking either logo: the row's app icon hides it, and the
// AppTopBar's brass mark hides it while shown and restores it while
// hidden. Persisted so the choice survives relaunch. Meaningless in a
// browser tab — DesktopTitleBar renders nothing there, and AppTopBar's
// mark is a toggle only when the customTitleBar capability is present.
const LS_TITLE_BAR_HIDDEN_KEY = 'collimate.titleBarHidden';

const load = (): boolean => {
    try { return localStorage.getItem(LS_TITLE_BAR_HIDDEN_KEY) === '1'; } catch { return false; }
};

interface TitleBarState {
    titleBarHidden: boolean;
    toggleTitleBar: () => void;
}

export const useTitleBarStore = create<TitleBarState>()((set) => ({
    titleBarHidden: load(),
    toggleTitleBar: () => set((s) => {
        const next = !s.titleBarHidden;
        try { localStorage.setItem(LS_TITLE_BAR_HIDDEN_KEY, next ? '1' : '0'); } catch { /* quota / SSR */ }
        return { titleBarHidden: next };
    }),
}));

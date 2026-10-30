import React, { createContext, useContext, useState, ReactNode, useMemo } from 'react';

export type FocusArea = 'nav' | 'main';
export type MainFocusMode = 'window' | 'files' | 'file-editor';

// The app controller reads the complete focus state; stack chrome subscribes
// only to the area that affects its focused appearance.
export interface FocusState {
    focusArea: FocusArea;
    selectedWindowId: string | null;
    mainFocusMode: MainFocusMode;
}

// Setters and action helpers. These are stable for the lifetime of the
// provider, so consumers that subscribe to FocusActionsContext never
// re-render due to focus state changes.
export interface FocusActions {
    setFocusArea: (area: FocusArea) => void;
    setSelectedWindowId: (id: string | null) => void;
    setMainFocusMode: (mode: MainFocusMode) => void;
}

export type FocusContextType = FocusState & FocusActions;

export const FocusContext = createContext<FocusContextType | undefined>(undefined);
export const FocusAreaContext = createContext<FocusArea | undefined>(undefined);
export const FocusActionsContext = createContext<FocusActions | undefined>(undefined);

export const FocusProvider = ({ children }: { children: ReactNode }) => {
    const [focusArea, setFocusArea] = useState<FocusArea>('nav');
    const [selectedWindowId, setSelectedWindowId] = useState<string | null>(null);
    const [mainFocusMode, setMainFocusMode] = useState<MainFocusMode>('window');
    // useState setters are stable, so the actions object is referentially
    // stable: consumers that only call setters never re-render on focus
    // state changes.
    const actionsValue = useMemo<FocusActions>(() => ({
        setFocusArea, setSelectedWindowId, setMainFocusMode,
    }), []);

    const stateValue = useMemo<FocusState>(() => ({
        focusArea, selectedWindowId, mainFocusMode,
    }), [focusArea, selectedWindowId, mainFocusMode]);

    const contextValue = useMemo<FocusContextType>(
        () => ({ ...stateValue, ...actionsValue }),
        [stateValue, actionsValue],
    );

    return (
        <FocusActionsContext.Provider value={actionsValue}>
            <FocusAreaContext.Provider value={focusArea}>
                <FocusContext.Provider value={contextValue}>
                    {children}
                </FocusContext.Provider>
            </FocusAreaContext.Provider>
        </FocusActionsContext.Provider>
    );
};

export const useFocus = () => {
    const context = useContext(FocusContext);
    if (!context) {
        throw new Error('useFocus must be used within a FocusProvider');
    }
    return context;
};

export const useFocusActions = (): FocusActions => {
    const ctx = useContext(FocusActionsContext);
    if (!ctx) throw new Error('useFocusActions must be used within a FocusProvider');
    return ctx;
};

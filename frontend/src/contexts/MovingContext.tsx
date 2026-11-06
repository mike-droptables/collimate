/**
 * MovingContext — global `isMoving` state for the canvas pan/zoom gesture.
 *
 * When the canvas is being panned or zoomed, `isMoving` flips to true.
 * All SandboxIframe wrappers consume this context to apply `pointer-events: none`,
 * preventing iframes from swallowing mouse drag events during canvas transitions.
 *
 * The state is driven only by the window events `canvas-pan-start` /
 * `canvas-pan-end`, which the pan sources dispatch directly.
 */

import React, { createContext, useContext, useMemo, useState, useEffect } from 'react';

interface MovingContextType {
  isMoving: boolean;
}

const MovingContext = createContext<MovingContextType>({
  isMoving: false,
});

export function MovingProvider({ children }: { children: React.ReactNode }) {
  const [isMoving, setIsMoving] = useState(false);

  // Listen for global events so non-React imperative triggers update this state
  useEffect(() => {
    const handleStart = () => setIsMoving(true);
    const handleEnd = () => setIsMoving(false);
    window.addEventListener('canvas-pan-start', handleStart);
    window.addEventListener('canvas-pan-end', handleEnd);
    return () => {
      window.removeEventListener('canvas-pan-start', handleStart);
      window.removeEventListener('canvas-pan-end', handleEnd);
    };
  },[]);

  const value = useMemo(() => ({ isMoving }), [isMoving]);

  return (
    <MovingContext.Provider value={value}>
      {children}
    </MovingContext.Provider>
  );
}

export function useMoving(): MovingContextType {
  return useContext(MovingContext);
}


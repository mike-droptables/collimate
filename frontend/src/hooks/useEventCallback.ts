import {useCallback,useLayoutEffect,useRef} from 'react';

/** Stable event identity with the latest committed implementation. */
export function useEventCallback<Args extends unknown[],Result>(callback:(...args:Args)=>Result):(...args:Args)=>Result {
    const latest=useRef(callback);
    useLayoutEffect(()=>{latest.current=callback});
    return useCallback((...args:Args)=>latest.current(...args),[]);
}

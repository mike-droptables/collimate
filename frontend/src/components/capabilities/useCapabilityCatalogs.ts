import { useEffect, useRef, useState } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useRunnerCatalog } from '@/stores/useRunnerStore';
import {
    fetchGenerators, fetchRendererPreferences, fetchRenderersWithCell, RENDERER_PREFS_CHANGED_EVENT,
    type GeneratorEntry, type RendererEntry,
} from '@/lib/api';
import { fetchCapabilityProblems, type CapabilityProblem } from '@/lib/api/capabilitiesStack';
import { fetchHandlers, type HandlerInfo } from '@/lib/definitionApi';
import { fetchRunnerGenerators, type RunnerEntry, type RunnerGenerator } from '@/lib/runnerApi';

/** One catalog of the manage view: its data once loaded (null while it
 *  loads or after a failure with nothing loaded before), why the last load
 *  failed, and a retry. */
export interface CatalogSlot<T> {
    data: T | null;
    error: string;
    retry: () => void;
}

export interface CapabilityCatalogs {
    generators: CatalogSlot<{ generators: GeneratorEntry[]; runnerGenerators: RunnerGenerator[] }>;
    runners: CatalogSlot<RunnerEntry[]>;
    renderers: CatalogSlot<{ renderers: RendererEntry[]; prefs: Record<string, string> }>;
    handlers: CatalogSlot<HandlerInfo[]>;
    problems: CatalogSlot<CapabilityProblem[]>;
}

const message = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

const loadGenerators = async () => {
    const [generators, runnerGenerators] = await Promise.all([fetchGenerators(), fetchRunnerGenerators()]);
    return { generators, runnerGenerators };
};
const loadRenderers = async () => {
    const [renderers, prefs] = await Promise.all([fetchRenderersWithCell(), fetchRendererPreferences()]);
    return { renderers, prefs: prefs ?? {} };
};

/** Load `load` for the channel whenever `key` changes, keeping only the
 *  latest answer: an older load that resolves late is dropped. */
function useCatalog<T>(channel: string, key: string, load: (channel: string) => Promise<T>, fallback: string): CatalogSlot<T> {
    const [state, setState] = useState<{ channel: string; data: T | null; error: string }>({ channel, data: null, error: '' });
    const [attempt, setAttempt] = useState(0);
    const loadSeq = useRef(0);
    useEffect(() => {
        if (!channel) return;
        const seq = ++loadSeq.current;
        load(channel).then(
            data => { if (seq === loadSeq.current) setState({ channel, data, error: '' }); },
            e => {
                if (seq !== loadSeq.current) return;
                setState(prev => ({ channel, data: prev.channel === channel ? prev.data : null, error: message(e, fallback) }));
            },
        );
    }, [channel, key, attempt, load, fallback]);
    const own = state.channel === channel;
    return { data: own ? state.data : null, error: own ? state.error : '', retry: () => setAttempt(n => n + 1) };
}

/**
 * Every catalog the Capabilities manage view lists, each loading on its
 * own: the generators (with their runner schemas), the runners (the shared
 * runner store), the renderers (with the channel's preferences), the
 * handlers and the problems listing. They reload once per change of the
 * Capabilities stack (`_capabilitiesFingerprint`) and again on every
 * renderer or handler mutation (RENDERER_PREFS_CHANGED_EVENT, which the
 * renderer rows and handler cards dispatch).
 */
export function useCapabilityCatalogs(): CapabilityCatalogs {
    const channel = useCollimateStore(s => s.activeChannel?.id ?? '');
    const fingerprint = useCollimateStore(s => s._capabilitiesFingerprint);
    const [changes, setChanges] = useState(0);
    useEffect(() => {
        const changed = () => setChanges(n => n + 1);
        window.addEventListener(RENDERER_PREFS_CHANGED_EVENT, changed);
        return () => window.removeEventListener(RENDERER_PREFS_CHANGED_EVENT, changed);
    }, []);
    const key = `${fingerprint}#${changes}`;
    const runnerCatalog = useRunnerCatalog();
    return {
        generators: useCatalog(channel, fingerprint, loadGenerators, 'Failed to load generators'),
        runners: { data: runnerCatalog.data, error: runnerCatalog.error ?? '', retry: () => { void runnerCatalog.refresh(); } },
        renderers: useCatalog(channel, key, loadRenderers, 'Failed to load renderers'),
        handlers: useCatalog(channel, key, fetchHandlers, 'Failed to load handlers'),
        problems: useCatalog(channel, key, fetchCapabilityProblems, 'Failed to load problems'),
    };
}

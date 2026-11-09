import { useCollimateStore } from '@/stores/useCollimateStore';

/** Immutable identity of the active channel, or empty when none is selected. */
export function currentChannelId(): string {
    return useCollimateStore.getState().activeChannel?.id ?? '';
}

// @vitest-environment jsdom
import { renderHook } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { useRunRequest } from './useRunRequest';
import { useCollimateStore } from '@/stores/useCollimateStore';

const openPickSession = vi.fn();
const clearGeneratorRunnerRequest = vi.fn(() => {
    useCollimateStore.setState({ pendingGeneratorRunnerStackId: null });
});
beforeEach(() => {
    vi.clearAllMocks();
    useCollimateStore.setState({
        pendingGeneratorRunnerStackId: 'fan-target',
        openPickSession, clearGeneratorRunnerRequest,
    });
});
it('F0958: the request hook opens the runners panel on the visible matching display cell and consumes the request (P02)', () => {
    renderHook(() => useRunRequest({ displayStackId: 'fan-target', cellId: 'real-cell', isFront: true }));
    expect(openPickSession).toHaveBeenCalledExactlyOnceWith('real-cell', 'runners');
    expect(clearGeneratorRunnerRequest).toHaveBeenCalledOnce();
});
it.each([
    { displayStackId: 'other', cellId: 'real-cell', isFront: true },
    { displayStackId: 'fan-target', cellId: 'real-cell', isFront: false },
])('F0958: another display or nonfront cell leaves the request pending', props => {
    renderHook(() => useRunRequest(props));
    expect(openPickSession).not.toHaveBeenCalled();
    expect(clearGeneratorRunnerRequest).not.toHaveBeenCalled();
});
it('F0958: a read-only matching host consumes the shortcut without opening an invisible panel', () => {
    renderHook(() => useRunRequest({ displayStackId: 'fan-target', cellId: 'real-cell', isFront: true, readOnly: true }));
    expect(openPickSession).not.toHaveBeenCalled();
    expect(clearGeneratorRunnerRequest).toHaveBeenCalledOnce();
});

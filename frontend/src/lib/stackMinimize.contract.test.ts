// @vitest-environment node
// Exercise the actual StackApp callback body: the Capabilities pill's
// toggle. Hiding writes no selection of its own (the selection invariant
// hands a hidden stack's selection to its neighbour, and a refused hide
// keeps both), and showing selects the stack only once it is restored.
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { expect, it, vi } from 'vitest';

const CHANNEL = '11111111-1111-4111-8111-111111111111';

/** The arrow passed to useCallback for `toggleCapabilitiesStack`, compiled
 *  over the names it closes on. */
function toggleFor(scope: {
    capabilitiesPlacement: { stack: { id: string; stack_type: string }; shown: boolean } | null;
    performAction: (p: Promise<unknown>) => Promise<unknown>;
    focusState: { selectedWindowId: string | null; setSelectedWindowId: (id: string) => void; setFocusArea: (area: string) => void };
    api: { minimizeStack: (...a: unknown[]) => Promise<unknown>; restoreMinimizedStack: (...a: unknown[]) => Promise<unknown> };
}): () => Promise<void> {
    const source = ts.createSourceFile('StackApp.tsx', readFileSync('src/components/StackApp.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    let arrow = '';
    const visit = (node: ts.Node) => {
        if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'toggleCapabilitiesStack'
            && node.initializer && ts.isCallExpression(node.initializer)) arrow = node.initializer.arguments[0].getText(source);
        ts.forEachChild(node, visit);
    };
    visit(source);
    expect(arrow).not.toBe('');
    const code = ts.transpileModule(`const callback = ${arrow};`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
    return new Function('capabilitiesPlacement', 'performAction', 'focusState', 'api', 'activeChannelId', `${code}; return callback;`)(
        scope.capabilitiesPlacement, scope.performAction, scope.focusState, scope.api, CHANNEL,
    );
}

const scope = (shown: boolean, performAction: (p: Promise<unknown>) => Promise<unknown>) => ({
    capabilitiesPlacement: { stack: { id: 'caps', stack_type: 'capabilities' }, shown },
    performAction,
    focusState: { selectedWindowId: 'caps', setSelectedWindowId: vi.fn(), setFocusArea: vi.fn() },
    api: { minimizeStack: vi.fn(async () => ({})), restoreMinimizedStack: vi.fn(async () => ({})) },
});

it('a refused Capabilities hide leaves selection unchanged__F0597', async () => {
    const s = scope(true, async () => null);
    await toggleFor(s)();
    expect(s.api.minimizeStack).toHaveBeenCalledWith(CHANNEL, 'caps');
    expect(s.focusState.setSelectedWindowId).not.toHaveBeenCalled();
});

it('a successful hide writes no selection either: the layout hands it to the neighbour', async () => {
    const s = scope(true, async p => p);
    await toggleFor(s)();
    expect(s.focusState.setSelectedWindowId).not.toHaveBeenCalled();
});

it('showing restores right of the selected stack, then selects it; a refused restore selects nothing', async () => {
    const shown = scope(false, async p => p);
    shown.focusState.selectedWindowId = 'left';
    await toggleFor(shown)();
    expect(shown.api.restoreMinimizedStack).toHaveBeenCalledWith(CHANNEL, 'caps', 'left');
    expect(shown.focusState.setSelectedWindowId).toHaveBeenCalledWith('caps');
    const refused = scope(false, async () => null);
    await toggleFor(refused)();
    expect(refused.focusState.setSelectedWindowId).not.toHaveBeenCalled();
});

it('does nothing before the channel has a Capabilities stack', async () => {
    const s = { ...scope(true, async p => p), capabilitiesPlacement: null };
    await toggleFor(s)();
    expect(s.api.minimizeStack).not.toHaveBeenCalled();
});

import { vi } from 'vitest';
import { fileFixture } from '@/test/fileFixtures';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { GeneratorEntry, RendererEntry } from '@/lib/api';
import type { HandlerInfo } from '@/lib/definitionApi';
import type { RunnerEntry, RunnerGenerator } from '@/lib/runnerApi';
import type { Cell, VirtualFile } from '@/lib/types';

// Synthetic capabilities: a generator 'synth-gen' with its runner
// 'synth-run', a user renderer and handler on '.memo', and one built-in
// renderer and handler on the Definitions card.

export const generator = (over: Partial<GeneratorEntry> = {}): GeneratorEntry => ({
    name: 'synth-gen', display_name: 'Synth generator', summary: 'Makes synthetic files.', cell_id: 'tools', group: 'Tools', group_icon: null,
    daemon: false, chat: false, config_folders: [], live_files_mode: 'none', version: '1.0', entrypoint: 'run.sh', ui_files: [], requires_docker: false, ...over,
});

export const runnerGenerator = (over: Partial<RunnerGenerator> = {}): RunnerGenerator => ({
    id: 'synth-gen', name: 'Synth generator', cell_id: 'tools', main_config: 'settings.yaml', fields: [], settings_files: {},
    account_requirements: {}, config_folders: [], enforces: [], configs: [],
    default: { runner: { name: '', description: 'The default description', tags: [], prompt: '', settings: {}, accounts: {}, folders: [], permissions: {}, packages: {} }, files: [] },
    ...over,
});

export const runner = (over: Partial<RunnerEntry> = {}): RunnerEntry => ({
    id: 'synth-run', name: 'Synth run', description: 'Runs it nightly.', tags: ['nightly'], generator: 'synth-gen', prompt: '', settings: {},
    accounts: {}, folders: [], permissions: {}, packages: {}, revision: 'r1', cell_id: 'tools', ...over,
});

export const renderer = (over: Partial<RendererEntry> = {}): RendererEntry => ({
    id: 'memo-view', name: 'Memo view', version: '1', summary: 'Shows memos.', extensions: ['.memo'], default_for: [], supports_diff: false, embed: 'native',
    content_hash: 'h', cell_id: 'views', available: true, ...over,
});

export const builtinRenderer = () => renderer({ id: 'plain', name: 'Plain', summary: '', extensions: ['.txt'], cell_id: 'defs', builtin: true });

export const handler = (over: Partial<HandlerInfo> = {}): HandlerInfo => ({
    id: 'memo', name: 'Memo', description: 'A memo with a heading.', types: [{ ext: '.memo' }], examples: [], cell_id: 'views', folder: 'memo',
    definition_path: 'memo/memo.handler', has_derived: false, built_hash: null, accepted: false, accepted_derived: null, disabled: false,
    clashes: [], accepted_skills: [], added_skills: [], builtin: false, uses_app_skills: false, ...over,
});

export const builtinHandler = () => handler({
    id: 'sheet', name: 'Sheet', description: 'Checks sheets.', types: [{ ext: '.sheet' }], cell_id: 'defs', folder: 'sheet',
    definition_path: 'sheet/sheet.handler', builtin: true, accepted: true, uses_app_skills: true,
});

export const MEMO_RENDERER = 'id: memo-view\nname: Memo view\nsummary: Shows memos.\nextensions:\n  - .memo\n';

export const file = (name: string, content: string, extra: Partial<VirtualFile> = {}) => fileFixture(name, content, { language: 'yaml', timestamp: 't1', ...extra });

/** A channel with a Capabilities stack (the Definitions card, then the
 *  'Tools' and 'Views' cells) and one standard stack. */
export function seedChannel(extra: Record<string, unknown> = {}) {
    const defs = { id: 'defs', name: 'Definitions', builtin: true, files: [] } as unknown as Cell;
    const tools = { id: 'tools', name: 'Tools', files: [] } as unknown as Cell;
    const views = { id: 'views', name: 'Views', files: [file('memo-view/memo.renderer', MEMO_RENDERER), file('memo/memo.handler', 'id: memo\n')] } as unknown as Cell;
    const work = { id: 'work', name: 'Work', files: [] } as unknown as Cell;
    const channel = {
        id: 'ch', name: 'ch', nav_stacks: [], nav_selection_id: null, main_selection_id: 'caps',
        main_stacks: [
            { id: 'caps', name: 'Capabilities', stack_type: 'capabilities' as const, cells: [defs, tools, views] },
            { id: 'w', name: 'Work', stack_type: 'standard' as const, cells: [work] },
        ],
    };
    useCollimateStore.setState({
        state: { active_channel_id: 'ch', channels: [channel] } as never, activeChannel: channel as never,
        cellCache: {}, cellHistory: {}, pickSessions: {}, cellLayouts: {}, runs: {}, lastFinishedByCell: {},
        capabilitiesManageRequest: null, _capabilitiesFingerprint: 'fp1',
        applyCellFiles: vi.fn(async () => true), applyOptimisticLayout: vi.fn(),
        ...extra,
    } as never);
    return channel;
}

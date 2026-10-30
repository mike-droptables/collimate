import { routeApiToFetchStub } from '@/test/apiTransport';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { duplicateRunner, promptGate, type RunnerGenerator } from './runnerApi';
import { accountMatches, accountOptionLabel, accountSettings, authLabel, type Account, type AccountRequirement } from './accountsApi';
import { requestJson } from './api/client';

const model: Account = { id: 'model', name: 'Work model', kind: 'model', service: 'custom', protocol: 'openai-compatible', endpoint: 'https://models.example/v1', model: 'chosen-model', capabilities: ['text'], auth_type: 'api_key', metadata: { harness: 'pydantic-ai' }, configured: true, credential_account_id: 'work' };

describe('saved account taxonomy', () => {
    it('uses the normalized saved harness without a client default __F0951', () => {
        expect(accountMatches({ ...model, metadata: {} }, { harness: 'pydantic-ai' })).toBe(false);
        expect(accountMatches({ ...model, metadata: { harness: 'future' } }, { harness: 'future' })).toBe(true);
    });
    it.each<[AccountRequirement, boolean]>([
        [{ kind: 'model', protocols: ['openai-compatible'], capabilities: ['text'] }, true],
        [{ kinds: ['model'], auth_types: ['api_key'], harnesses: ['pydantic-ai'] }, true],
        [{ kinds: ['model'], auth_types: ['cli'] }, false],
        [{ harnesses: ['codex'] }, false],
        [{ capabilities: ['images'] }, false],
        [{ protocols: [] }, false],
        [{ kind: 'account', kinds: ['model'] }, true],
    ])('matches all saved requirement axes: %j', (requirement, expected) => {
        expect(accountMatches(model, requirement)).toBe(expected);
    });

    it('takes a blank model as the harness default and excludes what the server marked unusable', () => {
        expect(accountMatches({ ...model, model: '' }, {})).toBe(true);
        expect(accountMatches({ ...model, model: '', model_configuration_error: 'Choose a model — this endpoint has no default model.' }, {})).toBe(false);
        expect(accountMatches({ ...model, configured: false }, {})).toBe(false);
        expect(accountMatches({ ...model, supported_harnesses: ['pydantic-ai'], metadata: { harness: 'codex' } }, { kind: 'model' })).toBe(false);
        const custom = { ...model, kind: 'custom', metadata: { custom_type: 'deployment' } };
        expect(accountMatches(custom, { kind: 'custom', custom_type: 'deployment' })).toBe(true);
        expect(accountMatches(custom, { kind: 'custom', custom_types: ['database'] })).toBe(false);
    });

    it('names the model a selection runs, and the harness default when it is blank', () => {
        expect(accountOptionLabel(model)).toBe('Work model · chosen-model · pydantic-ai');
        expect(accountOptionLabel({ ...model, model: '' })).toBe('Work model · harness default · pydantic-ai');
        expect(accountOptionLabel({ ...model, model: '', metadata: { harness: 'codex' } })).toBe('Work model · harness default · codex');
    });

    it.each<[string, Parameters<typeof authLabel>[1], string]>([
        ['api_key', { auth_labels: { api_key: 'Personal access token' } }, 'Personal access token'],
        ['api_key', undefined, 'API key'],
        ['basic', undefined, 'Username and password'],
        ['basic', { auth_labels: { api_key: 'Personal access token' } }, 'Username and password'],
        ['ssh_key', undefined, 'SSH key'],
        ['basic', { auth_labels: { basic: 'Username and token' } }, 'Username and token'],
        ['oauth_device', undefined, 'oauth device'],
    ])('names authentication method %s by the service catalog, else generically', (auth, service, expected) => {
        expect(authLabel(auth, service)).toBe(expected);
    });

    it('derives public settings only from a compatible selection and preserves missing bindings', () => {
        const generator: Pick<RunnerGenerator, 'fields' | 'account_requirements'> = { fields: [{ key: 'backend', type: 'text', default: 'pydantic-ai', account_role: 'model', account_property: 'metadata.harness' }], account_requirements: { model: { kind: 'model' } } };
        const saved = { backend: 'old', task: 'Keep this' };
        expect(accountSettings(generator, { model: model.id }, [model], saved)).toEqual({ backend: 'pydantic-ai', task: 'Keep this' });
        expect(accountSettings(generator, { model: model.id }, [{ ...model, metadata: { harness: 'codex' } }], saved).backend).toBe('codex');
        expect(accountSettings(generator, { model: 'deleted' }, [model], saved)).toEqual(saved);
        expect(accountSettings(generator, {}, [model], saved)).toEqual({ backend: 'pydantic-ai', task: 'Keep this' });
        expect(saved.backend).toBe('old');
    });
});

const gated = (over: Partial<RunnerGenerator>): Pick<RunnerGenerator, 'fields' | 'prompt_required' | 'prompt_show_if' | 'prompt_required_if'> => ({
    fields: [{ key: 'session', type: 'dropdown', options: [{ value: 'oneshot', label: 'One-shot' }, { value: 'interactive', label: 'Interactive' }], default: 'oneshot' }],
    ...over,
});

describe('prompt gate', () => {
    it('shows an unconditional prompt and requires it only when the flag is set', () => {
        expect(promptGate(gated({}), { session: 'oneshot' })).toEqual({ shown: true, required: false });
        expect(promptGate(gated({ prompt_required: true }), { session: 'oneshot' })).toEqual({ shown: true, required: true });
    });

    it('hides the prompt when its condition does not hold, and never requires a hidden prompt', () => {
        const generator = gated({ prompt_show_if: { session: 'oneshot' }, prompt_required_if: { session: 'oneshot' } });
        expect(promptGate(generator, { session: 'oneshot' })).toEqual({ shown: true, required: true });
        expect(promptGate(generator, { session: 'interactive' })).toEqual({ shown: false, required: false });
        expect(promptGate(gated({ prompt_required: true, prompt_show_if: { session: 'oneshot' } }), { session: 'interactive' })).toEqual({ shown: false, required: false });
    });

    it('treats a condition over a hidden referent as unmet', () => {
        const generator = {
            fields: [
                { key: 'session', type: 'dropdown' as const, default: 'interactive' },
                { key: 'surface', type: 'dropdown' as const, default: 'todo', show_if: { session: 'interactive' } },
            ],
            prompt_show_if: { surface: 'todo' },
        };
        expect(promptGate(generator, { session: 'interactive', surface: 'todo' }).shown).toBe(true);
        expect(promptGate(generator, { session: 'oneshot', surface: 'todo' }).shown).toBe(false);
    });
});

describe('requestJson errors', () => {
    let restore: () => void;
    beforeEach(() => { restore = routeApiToFetchStub(); });
    afterEach(() => { restore(); vi.unstubAllGlobals(); });
    const respond = (body: string, status: number) => vi.stubGlobal('fetch', vi.fn(async () => new Response(body, { status })));

    it('reports the status for a non-JSON error body', async () => {
        respond('Internal Server Error', 500);
        await expect(requestJson('/runners')).rejects.toThrow('Request failed (500)');
    });

    it('surfaces a string detail and the first validation message', async () => {
        respond(JSON.stringify({ detail: 'nope' }), 409);
        await expect(requestJson('/runners')).rejects.toThrow('nope');
        respond(JSON.stringify({ detail: [{ msg: 'field required' }] }), 422);
        await expect(requestJson('/runners')).rejects.toThrow('field required');
    });
});

it('duplicates a stored runner through the same free-name rule while retaining its source cell__F0946',async()=>{
 const runner={id:'writer',name:'Writer',description:'',tags:[],generator:'gen',prompt:'Exact prompt',settings:{},accounts:{},folders:[],permissions:{},packages:{}};
 const fetch=vi.fn(async(_url:unknown,init?:RequestInit)=>new Response(JSON.stringify(init?.method==='POST'?{runner,files:[],cell_id:'source'}:{runner,files:[{name:'bytes',content:'exact'}],cell_id:'source'}),{status:200}));vi.stubGlobal('fetch',fetch);
 const restore = routeApiToFetchStub();
 try {await duplicateRunner('writer',['Writer copy','writer COPY 2']);const call=fetch.mock.calls.find(([,init])=>init?.method==='POST')!;const body=JSON.parse(String(call[1]?.body));expect(body.runner.name).toBe('Writer copy 3');expect(body.cell_id).toBe('source');expect(body.files).toEqual([{name:'bytes',content:'exact'}]);expect(body.runner.prompt).toBe('Exact prompt');}finally{restore();vi.unstubAllGlobals();}
});

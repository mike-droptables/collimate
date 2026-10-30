"""Compiled canonical tokens stay immutable and name the skin vocabulary."""
from sdk_runtime_helpers import run_runtime


def test_actual_runtime_tokens_are_frozen_skin_variables_R01(client, tmp_path):
    run_runtime(client,tmp_path,r'''
assert.ok(Object.isFrozen(sdk.tokens)&&Object.isFrozen(sdk.tokens.color)&&Object.isFrozen(sdk.tokens.font));
assert.deepEqual(Object.keys(sdk.tokens),['color','font','radius','shadow','lift']);
assert.deepEqual(Object.keys(sdk.tokens.color),['canvas','sheet','sheet2','sheet3','line','line2','ink','ink2','ink3',
 'brass','brassSoft','teal','tealSoft','tealSheet','red','redSoft','green','codeBg','proseInk']);
assert.deepEqual(Object.keys(sdk.tokens.font),['ui','mono','prose']);
for(const value of [...Object.values(sdk.tokens.color),...Object.values(sdk.tokens.font),sdk.tokens.radius,sdk.tokens.shadow,sdk.tokens.lift])assert.match(value,/^var\(--[a-z0-9-]+\)$/);
assert.equal(sdk.tokens.color.sheet2,'var(--sheet-2)');assert.equal(sdk.tokens.color.codeBg,'var(--code-bg)');
assert.equal(sdk.tokens.font.prose,'var(--font-prose)');assert.equal(sdk.tokens.radius,'var(--radius)');
assert.throws(()=>{sdk.tokens.color.ink='#000';},TypeError);
''')


def test_sdk_var_names_are_the_app_skin_css_names_R01(client, tmp_path):
    """The SDK's THEME_VAR_NAMES and the app's THEME_CSS_VARS name the same
    tokens, so every value the host sends has a typed name in the contract."""
    import re
    from pathlib import Path
    themes = (Path(__file__).resolve().parents[2] / 'frontend/src/lib/themes.ts').read_text()
    table = themes.split('export const THEME_CSS_VARS = {', 1)[1].split('}', 1)[0]
    app_names = re.findall(r":\s*'(--[a-z0-9-]+)'", table)
    assert len(app_names) == 25
    run_runtime(client, tmp_path, 'assert.deepEqual([...sdk.THEME_VAR_NAMES].sort(),' + repr(sorted(app_names)).replace("'", '"') + ');')

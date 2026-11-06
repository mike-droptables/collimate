"""Drive the actual shipped content-swap function, preserving its live editor.

The slice stands alone, so the module-level replaceChangedRange it applies is
stubbed here; its in-place, out-of-history transaction is pinned in
renderers/documents/milkdown/milkdown.test.ts."""
import json
import subprocess

import pytest

from tests._workspace import RENDERERS


SOURCE = RENDERERS / 'documents/milkdown/main.tsx'


def refresh(incoming, *, last_raw='old raw', last_emitted='old normalized', fail=False, missing=False, newer=None, cancel=False, create_fail_once=False):
    code = SOURCE.read_text()
    start = code.index('        const replaceContent =')
    end = code.index('        const buildCrepe =', start)
    config = json.dumps({'incoming': incoming, 'raw': last_raw, 'emitted': last_emitted, 'fail': fail, 'missing': missing, 'newer': newer, 'cancel': cancel, 'create_fail_once': create_fail_once})
    script = '''const config=JSON.parse(process.argv[1]);
    const result={destroy:0,create:0,actions:[],errors:[]};
    let cancelled=false,swapping=false,pendingFresh=null,applyingExternal=false,recoveryPromise=null,pendingRecoveryFresh=null;
    const editorColRef={current:{}},lastFileRef={current:config.raw},lastEmittedRef={current:config.emitted};
    const normalized=text=>text.replace(/^- /m,'* ');
    const original={destroy(){result.destroy++},getMarkdown(){return this.markdown},markdown:config.emitted,
      editor:{action(fn){if(config.fail)throw Error('parser refused');fn({setMarkdown(text){original.markdown=normalized(text)}})}}};
    const crepeRef={current:config.missing?null:original};
    const replaceChangedRange=text=>ctx=>{result.actions.push({text});ctx.setMarkdown(text)};
    const setLoadError=error=>result.errors.push(error);
    let releaseCreate;const gate=new Promise(resolve=>{releaseCreate=resolve});
    const createWithFallback=async text=>{result.create++;await gate;if(config.create_fail_once&&result.create===1)throw Error('initial parser failure');original.markdown=normalized(text);return original};
    ''' + code[start:end] + '''
    const first=replaceContent(config.incoming);
    const second=config.newer==null?null:replaceContent(config.newer);
    if(config.cancel)cancelled=true;
    releaseCreate();await first;await second;
    console.log(JSON.stringify({...result,same:crepeRef.current===original,raw:lastFileRef.current,
      emitted:lastEmittedRef.current,applyingExternal}));'''
    process = subprocess.run(['node', '--input-type=module', '-e', script, config], capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def test_external_refresh_uses_live_editor_public_replacement__F1841():
    result = refresh('- changed')
    assert result['same'] and result['destroy'] == result['create'] == 0
    assert result['actions'] == [{'text': '- changed'}]
    assert result['emitted'] == '* changed'
    assert result['raw'] == '- changed'
    assert not result['applyingExternal']


def test_raw_equal_normalized_different_refresh_does_nothing__F1818_F1842():
    result = refresh('- unchanged', last_raw='- unchanged', last_emitted='* unchanged')
    assert result['same'] and result['destroy'] == result['create'] == 0
    assert result['actions'] == []


@pytest.mark.parametrize('fail', [False, True])
def test_refresh_clears_echo_guard_without_destroying_editor__F1841(fail):
    result = refresh('- changed', fail=fail)
    assert result['same'] and result['destroy'] == 0
    assert not result['applyingExternal']
    if fail:
        assert any(error and 'parser refused' in error for error in result['errors'])
        assert result['raw'] == 'old raw'


def test_failed_initial_create_recovers_and_coalesces_latest_content__F1841():
    result = refresh('- corrected', missing=True, newer='- latest')
    assert result['create'] == 1
    assert result['destroy'] == 0
    assert result['raw'] == '- latest'
    assert result['emitted'] == '* latest'
    assert not result['applyingExternal']


def test_cancelled_initial_recovery_destroys_unpublished_editor__F1841():
    result = refresh('- corrected', missing=True, cancel=True)
    assert result['create'] == 1
    assert result['destroy'] == 1
    assert result['raw'] == 'old raw'


def test_failed_recovery_retries_only_the_newer_queued_write__F1841():
    result = refresh('- invalid', missing=True, newer='- corrected', create_fail_once=True)
    assert result['create'] == 2
    assert result['raw'] == '- corrected'
    assert result['emitted'] == '* corrected'


def test_recovery_failure_has_no_unbounded_retry__F1841():
    result = refresh('- invalid', missing=True, create_fail_once=True)
    assert result['create'] == 1
    assert result['raw'] == 'old raw'
    assert any(error and 'initial parser failure' in error for error in result['errors'])

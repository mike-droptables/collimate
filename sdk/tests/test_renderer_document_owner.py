"""The host document owner alone chooses primary-file replacements."""
from tests.test_renderer_diff_follow import _run


def test_editing_mount_publishes_each_snapshot_and_never_refetches_primary(tmp_path):
    _run(tmp_path, r'''
const h = host();
h.api.isReadOnly = () => false;
const emitted = [];
h.api.reportChange = value => emitted.push(value);
const View = () => { seen.push(m.useCollimateFile()); return null; };
const unmount = await m.defineRenderer({ component: View }).mount({}, h.api);
last().saveContent('first key');
last().saveContent('second key');
assert.deepEqual(emitted, ['first key', 'second key']);
h.disk = 'competing unversioned read';
h.changed('notes.md');
await settle();
assert.equal(h.reads, 0, 'only the host may resolve a primary body revision');
assert.equal(last().content, 'second key');
h.push('explicitly resolved body');
assert.equal(last().content, 'explicitly resolved body');
assert.deepEqual(emitted, ['first key', 'second key'], 'host replacement is not an edit');
unmount();
assert.equal(h.pushes.size, 0);
''')


def test_diff_mount_uses_host_replacements_without_unversioned_reads(tmp_path):
    _run(tmp_path, r'''
const h = host();
const unmount = await renderer.mountDiff({}, h.api);
h.disk = 'unversioned body';
h.changed('notes.md');
await settle();
assert.equal(h.reads, 0);
assert.equal(last().content, 'after v1');
h.push('accepted current revision');
assert.equal(last().content, 'accepted current revision');
assert.equal(last().diffContent, 'before');
unmount();
''')

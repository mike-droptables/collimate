// The renderer-host conformance probe: an ordinary renderer package, served
// by the browser tests both as a built bundle and as a cell package the
// browser compiles (src/test/hostProbeServer.ts). Each mount records itself
// on its own window's `__hostProbe` and calls whatever member of its api the
// test names, from inside the host's realm, so a host's answer is what a
// renderer would see. It never calls api.ready() on its own.
export default {
    async mount(container, api) {
        const registry = (window.__hostProbe ??= { mounts: [] });
        const mark = document.createElement('span');
        mark.dataset.hostProbeMark = '';
        const record = {
            n: registry.mounts.length + 1,
            keys: Object.keys(api).sort(),
            cleaned: false,
            mark,
            call(name, ...args) {
                const member = api[name];
                return typeof member === 'function' ? member.apply(api, args) : member;
            },
            // A message this renderer's own window sends its host, as a
            // misbehaving renderer could.
            post(message) {
                window.parent.postMessage(message, '*');
            },
        };
        mark.textContent = `host probe ${record.n}`;
        container.append(mark);
        registry.mounts.push(record);
        return () => {
            record.cleaned = true;
            mark.remove();
        };
    },
};

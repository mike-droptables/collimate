"""Browser-only observation of real journal transactions, workers and HTTP saves.

The wrappers preserve the application promises and do not add a save path. Raw
receipts distinguish in-flight work from sequence lag; neither is called an
internal queue length.
"""

import json
import math


def percentiles(values):
    ordered = sorted(values)
    result = {"count": len(ordered)}
    if ordered:
        result.update({name: ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]
                       for name, fraction in (("p50", .5), ("p95", .95),
                                              ("p99", .99), ("max", 1))})
    return result


def instrumentation(marker):
    return "const p5Marker = " + json.dumps(marker) + ";\n" + r"""
(() => {
    const epoch = () => performance.timeOrigin + performance.now();
    const data = window.__p5 = {inputs:[], journals:[], workers:[], requests:[],
        pendingRequests:0, maxPendingRequests:0, pendingJournals:0,
        maxPendingJournals:0, wrapperMs:[], started:null};
    const noteInput = input => {if (data.started !== null) data.inputs.push(input);};
    window.addEventListener('message', event => {
        if (event.origin === location.origin && event.data?.type === 'P5_KEY')
            noteInput(event.data);
    });
    window.addEventListener('keydown', event => {
        if (event.key.length !== 1 || event.ctrlKey || event.altKey || event.metaKey) return;
        const input = {type:'P5_KEY', at:epoch(), key:event.key};
        if (parent === window) noteInput(input);
        else if (location.origin !== 'null') parent.postMessage(input, location.origin);
    }, true);
    const put = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put = function(value, ...args) {
        if (this.name !== 'documents' || !value?.record || data.started === null)
            return put.call(this, value, ...args);
        const began = performance.now(), record = value.record;
        const body = record.body || '', offset = body.lastIndexOf(p5Marker);
        const row = {at:epoch(), writer:record.writerId, path:record.identity.path,
            sequence:record.editSeq, savedSequence:record.savedSeq,
            materializedSequence:record.materializedSeq ?? record.editSeq,
            snapshotId:record.recovery?.snapshotId ?? null,
            plainSuffix:offset < 0 ? null : body.slice(offset + p5Marker.length).trimEnd(),
            durability:this.transaction.durability};
        data.journals.push(row);
        data.maxPendingJournals = Math.max(data.maxPendingJournals, ++data.pendingJournals);
        this.transaction.addEventListener('complete', () => {
            row.completed = epoch(); --data.pendingJournals;
        }, {once:true});
        this.transaction.addEventListener('abort', () => {
            row.aborted = epoch(); --data.pendingJournals;
        }, {once:true});
        try {return put.call(this, value, ...args);}
        finally {data.wrapperMs.push(performance.now() - began);}
    };
    const NativeWorker = window.Worker;
    window.Worker = class extends NativeWorker {
        constructor(...args) {
            super(...args); this.p5Pending = new Map();
            this.addEventListener('message', event => {
                const row = this.p5Pending.get(event.data?.requestId);
                if (row) {
                    row.completed = epoch(); row.result = event.data.type;
                    this.p5Pending.delete(event.data.requestId);
                }
            });
        }
        postMessage(message, ...args) {
            if (message?.type === 'materialize' && data.started !== null) {
                const row = {at:epoch(), requestId:message.requestId,
                    inFlight:this.p5Pending.size + 1};
                this.p5Pending.set(message.requestId, row); data.workers.push(row);
            }
            return super.postMessage(message, ...args);
        }
    };
    const xhrOpen = XMLHttpRequest.prototype.open;
    const xhrSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url, ...args) {
        this.p5MutationUrl = String(url).endsWith('/files/mutate');
        return xhrOpen.call(this, method, url, ...args);
    };
    XMLHttpRequest.prototype.send = function(body) {
        if (!this.p5MutationUrl || data.started === null) return xhrSend.call(this, body);
        const began = performance.now();
        const row = {at:epoch()}; data.requests.push(row);
        data.maxPendingRequests = Math.max(data.maxPendingRequests, ++data.pendingRequests);
        this.addEventListener('loadend', () => {
            row.completed = epoch(); row.status = this.status; --data.pendingRequests;
            try {row.receipt = this.responseType === 'json' ? this.response : JSON.parse(this.responseText);}
            catch (error) {row.receiptError = String(error);}
        }, {once:true});
        if (typeof body === 'string') {
            const operation = JSON.parse(body);
            row.operationId = operation.operation_id;
            row.mutations = operation.mutations.map(change => ({path:change.path,
                sequence:change.edit_seq, writer:change.writer_id}));
        }
        try {return xhrSend.call(this, body);}
        finally {data.wrapperMs.push(performance.now() - began);}
    };
    window.__p5Begin = () => {
        for (const key of ['inputs','journals','workers','requests','wrapperMs']) data[key].length = 0;
        data.pendingRequests = data.maxPendingRequests = data.pendingJournals = data.maxPendingJournals = 0;
        data.started = epoch();
        const metrics = window.__persistenceMetrics;
        if (metrics) {
            for (const key of ['inputs','journals','longTasks','receivedInputs']) metrics[key].length = 0;
            metrics.snapshotInputs = {};
        }
        window.__persistencePluginTimes = [];
    };
})();
"""


def durability(data, snapshot_inputs, *, kind):
    """Correlate each controlled edit with its first subsuming actual commit."""
    rows = [row for row in data['journals'] if 'completed' in row]
    samples = []
    if kind == 'milkdown':
        for identity, started in snapshot_inputs.items():
            view, sequence = identity.rsplit(':', 1)
            commits = [row['completed'] for row in rows if row['snapshotId']
                       and row['snapshotId'].rsplit(':', 1)[0] == view
                       and int(row['snapshotId'].rsplit(':', 1)[1]) >= int(sequence)]
            samples.append({'snapshotId': identity, 'at': started,
                            'durableAt': min(commits) if commits else None})
    else:
        prefix = ''
        for index, event in enumerate(data['inputs'], 1):
            prefix += event['key']
            commits = [row['completed'] for row in rows
                       if row['plainSuffix'] is not None and row['plainSuffix'].startswith(prefix)]
            samples.append({'input': index, 'at': event['at'],
                            'durableAt': min(commits) if commits else None})
    elapsed = [row['durableAt'] - row['at'] for row in samples if row['durableAt'] is not None]
    return {'inputs': len(samples), 'committed': len(elapsed),
            'keyToSubsumingCommitMs': percentiles(elapsed), 'samples': samples}

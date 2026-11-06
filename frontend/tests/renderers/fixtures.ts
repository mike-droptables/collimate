import type { APIRequestContext } from '@playwright/test';

export const rendererIds = [
  'assistant', 'audio', 'excalidraw', 'html', 'image', 'mermaid', 'milkdown',
  'monaco', 'pdf', 'renderer_example', 'run', 'schema_form', 'slides', 'table', 'terminal', 'text',
  'video', 'website', 'webview',
] as const;
export type RendererId = typeof rendererIds[number];
export const marker = (id: string) => `P3 ${id} ready`;
export type FixtureFile = { name: string; content?: string; language?: string; blob_hash?: string };

function pdf(): Buffer {
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  const stream = 'BT /F1 12 Tf 20 100 Td (P3 PDF ready) Tj ET\n';
  objects.push(`<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}endstream`);
  let result = '%PDF-1.4\n';
  const offsets = [0];
  objects.forEach((body, i) => {
    offsets.push(Buffer.byteLength(result));
    result += `${i + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xref = Buffer.byteLength(result);
  result += `xref\n0 ${offsets.length}\n0000000000 65535 f \n`;
  result += offsets.slice(1).map(n => `${String(n).padStart(10, '0')} 00000 n \n`).join('');
  result += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(result);
}

/** A renderer the SDK ships without a case here throws: a missing fixture is
 * a failure, never a skip. */
export async function fixtureFiles(id: string, request: APIRequestContext): Promise<FixtureFile[]> {
  const text = marker(id);
  const file = (name: string, content: string): FixtureFile => ({ name, content });
  if (id === 'audio' || id === 'video' || id === 'image' || id === 'pdf') {
    let body: Buffer;
    if (id === 'image') body = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j0uoAAAAASUVORK5CYII=', 'base64');
    else if (id === 'pdf') body = pdf();
    else {
      const response = await request.get(`/__renderer_rollout/media/${id}`);
      if (!response.ok()) throw new Error(`Synthetic ${id} fixture: HTTP ${response.status()}`);
      body = await response.body();
    }
    const upload = await request.post('/api/blob', { data: body, headers: { 'Content-Type': 'application/octet-stream' } });
    if (!upload.ok()) throw new Error(`CAS upload: HTTP ${upload.status()}`);
    const { blob_hash } = await upload.json();
    return [{ name: `sample.${({ audio: 'wav', video: 'webm', image: 'png', pdf: 'pdf' } as Record<string, string>)[id]}`, blob_hash, language: 'binary' }];
  }
  switch (id) {
    case 'renderer_example': return [
      file('sample.renderer_example', JSON.stringify({ renderer: 'candidate/preview.renderer', file: 'sample.txt' })),
      file('candidate/preview.renderer', 'id: p3-preview-local\nname: Local preview fixture\nsummary: Reads its sample through the sealed preview API.\nentrypoint: main.js\nembed: native\nextensions: [.txt]\n'),
      file('candidate/main.js', `export default {async mount(element, api) {
        const heading = document.createElement('h1');
        heading.textContent = await api.getContent();
        element.append(heading);
        return () => heading.remove();
      }};`),
      file('sample.txt', text),
    ];
    case 'text': return [file('sample.txt', text)];
    case 'milkdown': return [file('sample.md', `# ${text}\n\nA small paragraph.\n`)];
    case 'monaco': return [file('sample.ts', `// ${text}\nconst value = 1;\n`)];
    case 'mermaid': return [file('sample.mmd', `graph TD\n A[${text}] --> B[Done]`)];
    case 'slides': return [file('sample.slides.md', `# ${text}\n---\n# Second slide`)];
    case 'table': return [file('sample.tsv', `Name\tValue\n${text}\t2\nSecond row\t1\n`)];
    case 'schema_form': return [file('sample.set_yaml', `title: ${text}\nfields:\n  - key: greeting\n    type: string\n    label: Greeting\nvalues:\n  greeting: hello\n`)];
    case 'html': return [file('sample.html', `<!doctype html><h1>${text}</h1><p>Local document</p>`)];
    case 'excalidraw': return [file('sample.excalidraw', JSON.stringify({ type: 'excalidraw', version: 2, source: 'collimate', elements: [], appState: {}, files: {} }))];
    case 'run': return [file('sample.run', JSON.stringify({ v: 1, run_id: 'synthetic-rollout', label: text, runner: { id: 'synthetic', name: 'Synthetic runner' }, generator: 'synthetic', folder: '_runs/synthetic/', started_at: '2026-10-03T00:00:00Z', params: {} }))];
    case 'assistant': return [file('sample.chat', JSON.stringify({ chat_id: '', ws_path: '', token: '', label: text, active_thread: 'main', threads_dir: 'threads', threads: [{ id: 'main', title: 'Main', file: 'threads/main.json' }] })), file('threads/main.json', JSON.stringify({ messages: [{ role: 'assistant', content: 'P3 assistant transcript ready' }] }))];
    case 'terminal': return [file('sample.terminal', JSON.stringify({ terminal_id: 'synthetic-rollout', ws_path: '/__renderer_rollout/terminal', token: 'synthetic', label: text, buttons: [] }))];
    case 'website': return [file('sample.website', JSON.stringify({ url: '/__renderer_rollout/page', directUrl: '', buttons: [], loadingCover: false }))];
    case 'webview': {
      const response = await request.get('/__renderer_rollout/config');
      if (!response.ok()) throw new Error(`Synthetic page configuration: HTTP ${response.status()}`);
      const { page_url } = await response.json();
      return [file('sample.webview', JSON.stringify({ v: 4, tab: 'tab-1', url: page_url, device: 'desktop', viewport: null, control: { wsBase: '/__renderer_rollout/control/' }, requires: ['nativeGuests'], native: { partition: 'persist:collimate-default', device: 'desktop' }, buttons: [] }))];
    }
    default: throw new Error(`No fixture for renderer ${id}`);
  }
}

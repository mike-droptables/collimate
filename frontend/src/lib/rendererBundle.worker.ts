import { build, initialize, version } from 'esbuild-wasm/esm/browser.js';
import wasmURL from 'esbuild-wasm/esbuild.wasm?url';
import { sha256 } from '@noble/hashes/sha2.js';
import { createRendererCompiler } from '../../../sdk/scripts/renderer_bundle_core.mjs';
import contract from '../../../sdk/src/collimate/renderer_bundle_contract.json';

const compiler = createRendererCompiler({ contract, compiler: { build, version }, digest: sha256 });
let initialized: Promise<void> | undefined;
self.onmessage = async (event: MessageEvent) => {
  const { id, files, entrypoint } = event.data;
  try {
    initialized ??= initialize({ wasmURL, worker: false }).catch(error => { initialized = undefined; throw error; });
    await initialized;
    const result = await compiler.bundleRenderer({ files: new Map(files), entrypoint });
    self.postMessage({ id, result });
  } catch (error) {
    self.postMessage({ id, error: error instanceof Error ? error.message : String(error) });
  }
};

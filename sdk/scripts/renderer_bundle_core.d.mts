export interface RendererBundleContract {
  schema: number;
  esbuild_version: string;
  options: Record<string, unknown>;
  resolve_extensions: string[];
  external_prefixes: string[];
}
export interface RendererBundle {
  source_hash: string;
  code: string;
  bundle_hash: string;
}
export interface RendererCompiler {
  version: string;
  build(options: Record<string, unknown>): Promise<{outputFiles?: readonly {text: string}[]}>;
}
export function packagePath(value: string): string;
export function createRendererCompiler(options: {
  contract: RendererBundleContract;
  compiler: RendererCompiler;
  digest(bytes: Uint8Array): Uint8Array | Promise<Uint8Array>;
}): {
  rendererSourceHash(files: Map<string, Uint8Array>, entrypoint: string): Promise<string>;
  bundleRenderer(input: {files: Map<string, Uint8Array>; entrypoint: string}): Promise<RendererBundle>;
};

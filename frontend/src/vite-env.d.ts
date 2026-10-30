/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BUILD_MODE: string
  readonly VITE_REMOTE_API_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

// Renderer externals can be absolute runtime URLs.
declare module "https://*";
declare module "http://*";

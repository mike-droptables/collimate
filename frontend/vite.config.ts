import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  base: "./",
  build: {
    target: "esnext",
    chunkSizeWarningLimit: 2000,
    outDir: "dist",
    rolldownOptions: {
      onwarn(warning, defaultHandler) {
        // These dynamic imports break init-order cycles between the stores
        // (useChannelSettingsStore → the editing, renderer-styles and
        // keybinding stores; packageReports → useCollimateStore) or lazily
        // mount a pane other screens import statically (SettingsView →
        // KeyManager), so "won't get its own chunk" is the expected outcome.
        if (warning.code === "INEFFECTIVE_DYNAMIC_IMPORT") return;
        defaultHandler(warning);
      },
    },
  },
  worker: {
    format: "es",
  },
  server: {
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:5001',
        changeOrigin: true,
      },
    },
  },
});

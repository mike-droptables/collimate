import { defineConfig } from '@playwright/test';

// The bundled-font check: serves the built frontend (frontend/dist, from
// `npm run build`) from a local server the spec starts, with every request to
// any other origin refused.
export default defineConfig({
  testDir: './tests/fonts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  outputDir: '../tmp/fonts/playwright-results',
  reporter: [['list']],
  use: { viewport: { width: 1280, height: 800 } },
});

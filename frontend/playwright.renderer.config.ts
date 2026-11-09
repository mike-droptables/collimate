import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/renderers',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 60_000 },
  outputDir: '../tmp/p3/rollout/playwright-results',
  reporter: [['list'], ['json', { outputFile: '../tmp/p3/rollout/playwright-results.json' }]],
  use: {
    baseURL: process.env.COLLIMATE_RENDERER_TEST_URL ?? 'http://127.0.0.1:5113',
    viewport: { width: 1600, height: 1000 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
});

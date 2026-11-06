import { defineConfig } from 'vitest/config';

// The shell's own suites only: desktop/release/ holds built packages, whose
// server environment carries the bundled trees' sources (their *.test.ts
// included), and dist/ the bundle.
export default defineConfig({
  test: { include: ['tests/**/*.test.ts'] },
});

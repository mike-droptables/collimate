import { defineConfig } from "eslint/config";
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";

export default defineConfig(
  {
    ignores: [
      "dist/**",
      "coverage/**",
      "playwright-report/**",
      "test-results/**",
      "public/**",
    ],
  },
  {
    files: ["**/*.{ts,tsx,js,jsx,mjs}"],
    extends: [
      js.configs.recommended,
      ...tseslint.configs.recommended,
      reactHooks.configs.flat["recommended-latest"],
    ],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
    plugins: {
      "react-refresh": reactRefresh,
    },
    rules: {
      // Existing code carries intentional dependency suppressions; keep
      // exhaustive-deps advisory while rules-of-hooks stays enforced.
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      // React Compiler lint rules (new in react-hooks v7) flag patterns
      // the existing canvas/iframe code uses deliberately (manual ref
      // orchestration, set-state-in-effect for layout sync). Advisory
      // until the codebase migrates toward compiler compatibility.
      "react-hooks/set-state-in-effect": "warn",
      "react-hooks/refs": "warn",
      "react-hooks/immutability": "warn",
      "react-hooks/preserve-manual-memoization": "warn",
      "react-refresh/only-export-components": "warn",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      "@typescript-eslint/no-explicit-any": "warn",
    },
  },
  // ── The desktop seam ─────────────────────────────────────────────────
  // `window.collimateDesktop` is the ONLY global the Electron shell
  // installs (desktop/src/preload/index.ts). Everything the page does
  // with it goes through src/lib/desktop, which owns the type
  // declaration, the absent-in-a-browser degradation, and the narrow
  // surface renderers get. A stray `window.collimateDesktop` elsewhere is
  // a second, untyped, un-degraded reader of the shell — the exact drift
  // this rule exists to stop. Use isDesktop() / hasCap() / openExternal()
  // / desktopBridge() from '@/lib/desktop' instead.
  //
  // no-restricted-properties can't be used here: it only matches an
  // object node literally named `window`, missing both
  // `(window as X).collimateDesktop` and `globalThis.collimateDesktop`.
  // The MemberExpression selector below matches any `<expr>.collimateDesktop`.
  {
    files: ["**/*.{ts,tsx,js,jsx,mjs}"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "MemberExpression[property.name='collimateDesktop']",
          message:
            "window.collimateDesktop is confined to src/lib/desktop/. Import isDesktop/hasCap/openExternal/desktopBridge from '@/lib/desktop' instead.",
        },
        {
          selector:
            "MemberExpression[computed=true] > Literal[value='collimateDesktop']",
          message:
            "window['collimateDesktop'] is confined to src/lib/desktop/. Import from '@/lib/desktop' instead.",
        },
        {
          selector: "TSPropertySignature[key.name='collimateDesktop']",
          message:
            "The Window.collimateDesktop declaration lives in src/lib/desktop/index.ts — one owner for the shell contract.",
        },
        {
          selector: "ObjectPattern > Property[key.name='collimateDesktop']",
          message:
            "Destructuring collimateDesktop off the window is confined to src/lib/desktop/.",
        },
      ],
    },
  },
  {
    // Exemptions:
    //   src/lib/desktop/**   — the owning module: the declare-global block
    //                          and the only reads of the raw bridge.
    //   src/test/desktopShell.ts — the vitest stub whose whole job is to
    //                          INSTALL and remove the global.
    //   tests/electron/**    — Playwright evaluates in the shell's page
    //                          context; the raw global IS the thing under
    //                          test there.
    // This override turns no-restricted-syntax off wholesale for these
    // paths — fine while the seam rule is its only entry; if more
    // restrictions are ever added above, re-list the survivors here.
    files: [
      "src/lib/desktop/**",
      "src/test/desktopShell.ts",
      "tests/electron/**",
    ],
    rules: { "no-restricted-syntax": "off" },
  },
);

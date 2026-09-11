import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    // Exclude Playwright E2E specs from the vitest runner — those are
    // collected by `playwright.config.ts` and run separately via
    // `bunx playwright test`. Without this exclusion vitest tries to
    // import them as vitest tests, fails (no `playwright/test` globals),
    // and the whole suite reports a FAIL even though no test ran.
    exclude: [
      'node_modules/**',
      'dist/**',
      '.next/**',
      'e2e/**',
      'playwright.config.ts',
    ],
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
})

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
    pool: 'forks',
    poolOptions: {
      forks: { singleFork: true, maxForks: 1, minForks: 1 },
    },
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

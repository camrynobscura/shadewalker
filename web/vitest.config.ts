import { defineConfig } from 'vitest/config'

// Pure-logic + hook unit tests, separate from playwright.config.ts's
// browser-driven e2e suite -- one config file per tool, same pattern.
// Colocated *.test.ts(x) files (Vitest's own convention) are what this
// picks up; *.spec.ts under web/e2e/ belongs to Playwright, not this.
export default defineConfig({
  test: {
    environment: 'jsdom',
    // Node 22+ ships its own experimental localStorage/sessionStorage
    // globals, undefined unless --localstorage-file is set — and they
    // shadow jsdom's real Storage when vitest populates the test global
    // (globalThis is window here). Turn the experiment off so jsdom's
    // storage wins; without this, localStorage is undefined in every
    // test.
    execArgv: ['--no-experimental-webstorage'],
    setupFiles: ['./vitest.setup.ts'],
    // Vitest's default include pattern also matches *.spec.ts, which would
    // otherwise sweep up web/e2e/'s Playwright specs -- those use a
    // completely different test() API and would fail to run here.
    include: ['src/**/*.test.{ts,tsx}'],
  },
})

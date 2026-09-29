import { defineConfig, devices } from '@playwright/test'

// Accessibility + one smoke path for the frontend, run against the real
// production build (not `vite dev`) so results match what Lighthouse
// audits against per CLAUDE.md. Needs both servers a real route depends
// on: the FastAPI backend (loaded with the pilot tile) and the built
// frontend. `vite preview` proxies /route, /health, /coverage to the
// backend via the same `server.proxy` block vite.config.ts already
// defines for `vite dev` — confirmed empirically, no separate config
// needed here.
//
// The backend command copies the committed pilot fixture
// (tests/fixtures/pilot.json.gz) into its own directory and points
// SHADEWALKER_EXPORT_DIR at it before starting uvicorn, rather than loading
// data/export/ directly — that directory holds the real citywide export once
// the pipeline has run, and these specs are written against the small,
// deterministic pilot fixture specifically (see pipeline/config.py's
// EXPORT_DIR comment).
//
// DEDICATED PORTS, AND reuseExistingServer: false. Both matter, and the
// second is why the first exists.
//
// This tier used to reuse whatever was already listening on 8000. When that
// was a citywide dev server, the specs ran against the WRONG GRAPH and said
// nothing about it: the out-of-coverage spec failed because midtown really is
// in coverage for the whole city, which looks exactly like a real regression.
// It cost two debugging detours in one session on 2026-08-24, the second
// AFTER the trap had been written down — so relying on whoever runs the suite
// to remember was already proven insufficient.
//
// Reusing a stale frontend preview is the same class of bug and quieter
// still: the specs would exercise an old build with no sign anything was
// wrong.
//
// Turning reuse off alone would trade a silent wrong-data failure for a loud
// port-collision failure — better, but it would still mean stopping your dev
// server to run tests. Dedicated ports remove the conflict instead, so the
// tier is hermetic and your dev environment keeps running beside it.
// SHADEWALKER_API_URL points the built frontend at the test backend;
// vite.config.ts reads it and falls back to 8000 for ordinary development.
//
// SHADEWALKER_NOW pins the backend's "now" to July 15 at noon. The app sends
// no time, so without it every run tested whatever the wall clock said --
// and since `night-shade` a run after dark gets four identical routes at
// 100% shade, where the shade specs pass without testing anything. Night
// has its own spec, which asks for 02:00 explicitly (night.spec.ts).
const API_PORT = 8001
const PINNED_NOW = '2026-07-15T12:00'
const WEB_PORT = 4173

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  webServer: [
    {
      command:
        'mkdir -p data/e2e_export && cp tests/fixtures/pilot.json.gz data/e2e_export/ && ' +
        `SHADEWALKER_EXPORT_DIR="$(pwd)/data/e2e_export" SHADEWALKER_DISABLE_RATE_LIMIT=1 SHADEWALKER_NOW=${PINNED_NOW} uv run uvicorn server.app:app --port ${API_PORT}`,
      cwd: '..',
      url: `http://localhost:${API_PORT}/health`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: `npm run build && npm run preview -- --port ${WEB_PORT} --strictPort`,
      url: `http://localhost:${WEB_PORT}`,
      env: { SHADEWALKER_API_URL: `http://localhost:${API_PORT}` },
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    // CI keeps the trace of every failed test (uploaded by ci.yml): no
    // retries there, so 'on-first-retry' would never record one.
    trace: process.env.CI ? 'retain-on-failure' : 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    // Safari's engine, for what Chromium can't see: a tapped radio isn't
    // focused there (focus goes to the panel), which once made the time
    // pill's menu close on the tap meant to pick (2026-09-29).
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
      testMatch: 'time-picker.spec.ts',
    },
  ],
})

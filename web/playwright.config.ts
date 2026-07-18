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
// The backend command copies the pilot tile into its own directory and
// points SHADEWALKER_TILES_DIR at it before starting uvicorn, rather than
// loading data/tiles/ directly — that directory can also hold real
// Brooklyn/Manhattan tiles once the pipeline's done real borough work,
// and these specs are written against the small, deterministic pilot
// tile specifically (see pipeline/config.py's TILES_DIR comment).
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  webServer: [
    {
      command:
        'mkdir -p data/e2e_tiles && cp data/tiles/pilot.json.gz data/e2e_tiles/ && ' +
        'SHADEWALKER_TILES_DIR="$(pwd)/data/e2e_tiles" uv run uvicorn server.app:app --port 8000',
      cwd: '..',
      url: 'http://localhost:8000/health',
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: 'npm run build && npm run preview -- --port 4173 --strictPort',
      url: 'http://localhost:4173',
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
  use: {
    baseURL: 'http://localhost:4173',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})

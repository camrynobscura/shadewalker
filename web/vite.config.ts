import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Where the frontend forwards API calls. Defaults to the ordinary dev
// backend; playwright.config.ts overrides it so the e2e tier can run its own
// backend on its own port without colliding with a dev server you already
// have up (a tier that reuses whatever is listening on 8000 silently tests
// against a citywide server's data instead of the pilot fixture).
const API_TARGET = process.env.SHADEWALKER_API_URL ?? 'http://localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      // Two entries: the app, and the About page (its own React entry) --
      // served by the same static mount in production.
      input: { main: 'index.html', about: 'about.html' },
    },
  },
  server: {
    // Dev-only: forward API calls to the FastAPI server so the frontend can
    // fetch("/route?...") with no CORS setup and no hardcoded host. In
    // production FastAPI serves the built files itself, so the same relative
    // URLs keep working unchanged.
    //
    // `vite preview` reuses this block: Vite falls back to server.proxy when
    // preview.proxy is undefined, which is what lets the e2e tier proxy at
    // all without a second copy of these rules.
    proxy: {
      '/route': API_TARGET,
      '/health': API_TARGET,
      '/coverage': API_TARGET,
      '/geocode': API_TARGET, // prefix match — also covers /geocode/reverse
    },
  },
  preview: {
    // Phone testing through a cloudflared quick tunnel (`cloudflared
    // tunnel --url http://localhost:4173`; installed globally via npm) —
    // the HTTPS context geolocation requires, which the LAN preview
    // can't provide.
    // Vite's host check (DNS-rebinding protection) rejects the tunnel's
    // random hostname without this; the leading dot allows any
    // *.trycloudflare.com, since the name changes every run.
    allowedHosts: ['.trycloudflare.com'],
  },
})

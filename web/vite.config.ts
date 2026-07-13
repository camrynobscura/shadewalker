import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Dev-only: forward API calls to the FastAPI server so the frontend can
    // fetch("/route?...") with no CORS setup and no hardcoded host. In
    // production FastAPI serves the built files itself, so the same relative
    // URLs keep working unchanged.
    proxy: {
      '/route': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/coverage': 'http://localhost:8000',
    },
  },
})

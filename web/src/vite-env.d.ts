/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** CARTO basemaps key (web/.env.local, gitignored). Optional on
   * purpose: keyless builds still work, CARTO just watermarks the
   * tiles — so dev/e2e/CI never hard-depend on a local secret. */
  readonly VITE_CARTO_KEY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend base URL. Set → the data layer calls live endpoints; unset → fixtures. */
  readonly VITE_API_BASE?: string;
  /** The owner whose conversations are read/written (no auth in v1). Default "local". */
  readonly VITE_OWNER_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// evidence-harness-campaign.md W6 — the e2e bridge metadata object.
// Only present in DEV mode or when `localStorage.disco_e2e === '1'`.
interface Window {
  __DISCO_E2E__?: import("./lib/e2eBridge").DiscoE2EState;
}

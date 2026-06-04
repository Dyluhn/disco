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

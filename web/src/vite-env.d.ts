/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_POST_V8_ABC_SELECTION_MODE?: string;
  readonly VITE_POST_V8_ABC_SELECTION_CANDIDATE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

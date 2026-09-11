/** Reference Packs — the user's persistent, reusable file collections (agent-server owned). */

export type ReferencePackFileState = "ready" | "asset_only" | "unreadable";

export interface ReferencePackFile {
  name: string;
  media_type: string;
  bytes: number;
  sha256: string;
  state: ReferencePackFileState;
}

export interface ReferencePackSummary {
  pack_id: string;
  name: string;
  description: string;
  file_count: number;
  total_bytes: number;
  created_at: string;
  updated_at: string;
  digest: string;
}

export interface ReferencePackDetail extends ReferencePackSummary {
  files: ReferencePackFile[];
}

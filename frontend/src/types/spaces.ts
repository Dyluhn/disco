export interface SpaceSummary {
  space_id: string;
  name: string;
  description: string;
  created_at: string;
  doc_count: number;
  byte_count: number;
}

export interface SpaceDocument {
  document_id: string;
  name: string;
  media_type: string;
  byte_count: number;
  passage_count: number;
  created_at: string;
}

export interface SpaceDetail extends SpaceSummary {
  documents: SpaceDocument[];
}

export interface SpacesList {
  spaces: SpaceSummary[];
  status: "ok" | "no_runtime";
}

export interface CreateSpaceInput {
  name: string;
  description?: string;
}

export interface SpaceUploadResult {
  saved: SpaceDocument[];
  rejected: { name: string; reason: string }[];
  space: SpaceDetail;
}

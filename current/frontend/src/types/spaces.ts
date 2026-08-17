import type { ConversationSummary } from "./conversation";

export interface SpaceSummary {
  space_id: string;
  name: string;
  description: string;
  created_at: string;
  member_count: number;
}

export interface SpaceDetail extends SpaceSummary {
  members: ConversationSummary[];
}

export interface SpacesList {
  spaces: SpaceSummary[];
  status: "ok" | "no_runtime";
}

export interface CreateSpaceInput {
  name: string;
  description?: string;
}

export interface RenameSpaceInput {
  spaceId: string;
  name?: string;
  description?: string;
}

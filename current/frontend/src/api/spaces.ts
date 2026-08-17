import { agentGet, agentLive, agentSend, fixtureDelay } from "./client";
import type {
  CreateSpaceInput,
  RenameSpaceInput,
  SpaceDetail,
  SpaceSummary,
  SpacesList,
} from "@/types/spaces";

let fixtureSpaces: SpaceDetail[] = [];

export async function listSpaces(): Promise<SpacesList> {
  if (!agentLive()) {
    await fixtureDelay();
    return { spaces: fixtureSpaces.map(toSummary), status: "ok" };
  }
  return agentGet<SpacesList>("/api/spaces");
}

export async function createSpace(input: CreateSpaceInput): Promise<SpaceDetail> {
  if (!agentLive()) {
    await fixtureDelay();
    const now = new Date().toISOString();
    const space: SpaceDetail = {
      space_id: `space_fixture_${Date.now()}`,
      name: input.name.trim(),
      description: input.description?.trim() ?? "",
      created_at: now,
      member_count: 0,
      members: [],
    };
    fixtureSpaces = [space, ...fixtureSpaces];
    return space;
  }
  const res = await agentSend<{ space: SpaceDetail }>("POST", "/api/spaces", input);
  return normalizeDetail(res.space);
}

export async function getSpace(spaceId: string): Promise<SpaceDetail> {
  if (!agentLive()) {
    await fixtureDelay();
    const found = fixtureSpaces.find((s) => s.space_id === spaceId);
    if (!found) throw new Error("space not found");
    return found;
  }
  const res = await agentGet<{ space: SpaceDetail }>(`/api/spaces/${encodeURIComponent(spaceId)}`);
  return normalizeDetail(res.space);
}

export async function renameSpace(input: RenameSpaceInput): Promise<SpaceDetail> {
  const { spaceId, ...body } = input;
  if (!agentLive()) {
    await fixtureDelay();
    const found = fixtureSpaces.find((s) => s.space_id === spaceId);
    if (!found) throw new Error("space not found");
    const updated: SpaceDetail = {
      ...found,
      name: body.name?.trim() || found.name,
      description:
        body.description === undefined ? found.description : body.description.trim(),
    };
    fixtureSpaces = fixtureSpaces.map((s) => (s.space_id === spaceId ? updated : s));
    return updated;
  }
  const res = await agentSend<{ space: SpaceDetail }>(
    "PATCH",
    `/api/spaces/${encodeURIComponent(spaceId)}`,
    body,
  );
  return normalizeDetail(res.space);
}

export async function deleteSpace(spaceId: string): Promise<{ space_id: string }> {
  if (!agentLive()) {
    await fixtureDelay();
    fixtureSpaces = fixtureSpaces.filter((s) => s.space_id !== spaceId);
    return { space_id: spaceId };
  }
  await agentSend("DELETE", `/api/spaces/${encodeURIComponent(spaceId)}`);
  return { space_id: spaceId };
}

function toSummary(space: SpaceDetail): SpaceSummary {
  return {
    space_id: space.space_id,
    name: space.name,
    description: space.description,
    created_at: space.created_at,
    member_count: space.member_count,
  };
}

function normalizeDetail(space: SpaceDetail): SpaceDetail {
  return {
    ...space,
    members: space.members.map((member) => ({
      ...member,
      title: member.title ?? "(untitled)",
    })),
  };
}

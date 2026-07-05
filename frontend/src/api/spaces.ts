import { agentGet, agentHttpBase, agentLive, agentSend, fixtureDelay } from "./client";
import type {
  CreateSpaceInput,
  SpaceDetail,
  SpaceSummary,
  SpacesList,
  SpaceUploadResult,
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
      doc_count: 0,
      byte_count: 0,
      documents: [],
    };
    fixtureSpaces = [space, ...fixtureSpaces];
    return space;
  }
  const res = await agentSend<{ space: SpaceDetail }>("POST", "/api/spaces", input);
  return res.space;
}

export async function getSpace(spaceId: string): Promise<SpaceDetail> {
  if (!agentLive()) {
    await fixtureDelay();
    const found = fixtureSpaces.find((s) => s.space_id === spaceId);
    if (!found) throw new Error("space not found");
    return found;
  }
  const res = await agentGet<{ space: SpaceDetail }>(`/api/spaces/${encodeURIComponent(spaceId)}`);
  return res.space;
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

export async function uploadSpaceDocuments(
  spaceId: string,
  files: File[],
): Promise<SpaceUploadResult> {
  if (!agentLive()) {
    await fixtureDelay();
    throw new Error("Space document upload requires a live agent server.");
  }
  const fd = new FormData();
  for (const file of files) fd.append("files", file);
  const res = await fetch(
    `${agentHttpBase()}/api/spaces/${encodeURIComponent(spaceId)}/documents`,
    { method: "POST", body: fd },
  );
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const body = await res.json();
      detail = body.detail?.message || body.detail?.reason || JSON.stringify(body.detail);
    } catch {
      // keep status
    }
    throw new Error(`Upload failed (${res.status}): ${detail}`);
  }
  return (await res.json()) as SpaceUploadResult;
}

function toSummary(space: SpaceDetail): SpaceSummary {
  return {
    space_id: space.space_id,
    name: space.name,
    description: space.description,
    created_at: space.created_at,
    doc_count: space.doc_count,
    byte_count: space.byte_count,
  };
}

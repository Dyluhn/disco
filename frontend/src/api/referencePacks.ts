/**
 * Data-access for the user's Reference Packs (agent-server `/api/reference-packs`).
 * Creation is the Agent's `create_reference_pack` action; this module lists,
 * edits and deletes what exists. Offline (no agent base) → an empty library.
 */
import { agentFetch, agentGet, agentLive, agentSend } from "./client";
import type { ReferencePackDetail, ReferencePackSummary } from "@/types/referencePacks";

export async function listReferencePacks(): Promise<ReferencePackSummary[]> {
  if (!agentLive()) return [];
  const res = await agentGet<{ packs: ReferencePackSummary[] }>("/api/reference-packs");
  return res.packs;
}

export async function getReferencePack(packId: string): Promise<ReferencePackDetail> {
  const res = await agentGet<{ pack: ReferencePackDetail }>(`/api/reference-packs/${packId}`);
  return res.pack;
}

export async function updateReferencePack(
  packId: string,
  patch: { name?: string; description?: string },
): Promise<ReferencePackDetail> {
  const res = await agentSend<{ pack: ReferencePackDetail }>(
    "PATCH",
    `/api/reference-packs/${packId}`,
    patch,
  );
  return res.pack;
}

/** Add files; a file with an existing name replaces it. */
export async function addReferencePackFiles(
  packId: string,
  files: File[],
): Promise<ReferencePackDetail> {
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  const res = await agentFetch(`/api/reference-packs/${packId}/files`, {
    method: "POST",
    body: fd,
  });
  const body = (await res.json()) as { pack?: ReferencePackDetail; detail?: { message?: string } };
  if (!res.ok || !body.pack) {
    throw new Error(body.detail?.message ?? `Upload failed (${res.status})`);
  }
  return body.pack;
}

export async function removeReferencePackFile(
  packId: string,
  name: string,
): Promise<ReferencePackDetail> {
  const res = await agentSend<{ pack: ReferencePackDetail }>(
    "DELETE",
    `/api/reference-packs/${packId}/files/${encodeURIComponent(name)}`,
  );
  return res.pack;
}

export async function deleteReferencePack(packId: string): Promise<void> {
  await agentSend("DELETE", `/api/reference-packs/${packId}`);
}

export function referencePackFilePath(packId: string, name: string): string {
  return `/api/reference-packs/${packId}/files/${encodeURIComponent(name)}`;
}

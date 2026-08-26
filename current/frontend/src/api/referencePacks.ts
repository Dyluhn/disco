import { agentGet, agentLive, agentMultipart, agentSend, ApiError, fixtureDelay } from "./client";

/** Whether the agent-server reference-pack routes are available. Components
 * use this capability through this API module rather than reaching into the
 * transport boundary directly. */
export function referencePacksAvailable(): boolean {
  return agentLive();
}

export interface ReferencePackFile {
  name: string;
  path?: string;
  media_type: string;
  size: number;
  sha256: string;
}

export interface ReferencePackVersion {
  id: string;
  version_id?: string;
  description: string;
  files: ReferencePackFile[];
  content_sha256: string;
  total_bytes: number;
  created_at: string;
}

export interface ReferencePack {
  id: string;
  pack_id?: string;
  name: string;
  description: string;
  current_version_id: string;
  current: ReferencePackVersion;
  files: ReferencePackFile[];
  created_at: string;
  updated_at: string;
}

export interface ReferencePackFileInput {
  path: string;
  name?: string;
  media_type?: string;
}

export interface ReferencePackDraft {
  name: string;
  description?: string;
  files: ReferencePackFileInput[];
  conversation_id: string;
}

export interface ReferencePackBinding {
  id: string;
  binding_id?: string;
  conversation_id: string;
  packs: Array<{
    pack_id: string;
    version_id: string;
    name: string;
    description: string;
    files: ReferencePackFile[];
    content_sha256: string;
  }>;
  pack_ids: string[];
  version_ids: string[];
  created_at: string;
}

export async function listReferencePacks(): Promise<ReferencePack[]> {
  if (!agentLive()) {
    await fixtureDelay();
    return [];
  }
  const response = await agentGet<{ packs: ReferencePack[] }>("/api/reference-packs");
  return response.packs;
}

export async function createReferencePack(draft: ReferencePackDraft): Promise<ReferencePack> {
  return agentSend<ReferencePack>("POST", "/api/reference-packs", draft);
}

export async function updateReferencePack(
  id: string,
  patch: Partial<ReferencePackDraft>,
): Promise<ReferencePack> {
  return agentSend<ReferencePack>(
    "PATCH",
    `/api/reference-packs/${encodeURIComponent(id)}`,
    patch,
  );
}

export async function updateReferencePackFiles(
  id: string,
  files: File[],
  remove: string[] = [],
): Promise<ReferencePack> {
  const form = new FormData();
  for (const file of files) {
    const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
    form.append("files", file, relative || file.name);
  }
  for (const path of remove) form.append("remove", path);
  return agentMultipart<ReferencePack>(
    "PUT",
    `/api/reference-packs/${encodeURIComponent(id)}/files`,
    form,
  );
}

export async function deleteReferencePack(id: string): Promise<{ id: string; deleted: boolean }> {
  return agentSend<{ id: string; deleted: boolean }>(
    "DELETE",
    `/api/reference-packs/${encodeURIComponent(id)}`,
  );
}

export async function bindReferencePacks(
  conversationId: string,
  selections: Array<{ pack_id: string; version_id: string; content_sha256: string }>,
): Promise<ReferencePackBinding> {
  return agentSend<ReferencePackBinding>(
    "POST",
    `/api/conversations/${encodeURIComponent(conversationId)}/reference-packs/bind`,
    { selections },
  );
}

/** Build admission seam: bind the selected mutable-head identities first, then
 * allow the caller to kick the conversation. Keeping the ordering here makes a
 * first-turn race impossible to reintroduce in a UI refactor. A bind error is
 * intentionally allowed to reject; the caller must not submit in that case. */
export async function bindReferencePacksBeforeSubmit(
  selected: ReferencePack[],
  ensureConversation: (() => Promise<string | null>) | undefined,
  submit: () => void,
): Promise<void> {
  if (selected.length === 0 || !referencePacksAvailable()) {
    submit();
    return;
  }
  const conversationId = await ensureConversation?.();
  if (!conversationId) throw new Error("Reference Packs could not be attached to this Build.");
  await bindReferencePacks(
    conversationId,
    selected.map((pack) => ({
      pack_id: pack.id,
      version_id: pack.current_version_id,
      content_sha256: pack.current.content_sha256,
    })),
  );
  submit();
}

export async function getReferencePackBinding(
  conversationId: string,
): Promise<ReferencePackBinding | null> {
  if (!agentLive()) return null;
  try {
    return await agentGet<ReferencePackBinding>(
      `/api/conversations/${encodeURIComponent(conversationId)}/reference-packs/bind`,
    );
  } catch (error) {
    // A conversation without selected packs is a normal state, not an error.
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

/**
 * The Build (agent) surface data-access layer — the ONE place that talks to the
 * agent-server's conversation WebSocket + REST. Components never import this; only the
 * Build hooks do (the data-flow discipline). Live when VITE_AGENT_BASE is set; otherwise
 * it replays the in-repo agent-trace fixture (offline/tests/screenshots), including the
 * confirmation-gate pause that confirm/reject resume.
 *
 * Module-size decomposition (PKG-12-FE-BUILD/TS-0002): the offline fixture machinery,
 * the live WebSocket transport, the deck-editor API, and the share-link API each moved
 * to `./agentParts/*` and are recomposed here via import + re-export, so every existing
 * import path (`@/api/agent`) and export name is unchanged.
 */

import { FIXTURE_CID } from "@/fixtures/agentTrace";
import { FIXTURE_DEEP_CID } from "@/fixtures/deepResearchTrace";
import type { DriverModels, PreviewInfo, WSClientFrame, WSServerFrame } from "@/types/agent";
import { subscribeFixture, subscribeDeepFixture } from "./agentParts/fixtures";
import type { JsonPatchOp, LoweredDeck } from "@/components/build/editor/types";
import {
  getDeckForEditor as deckGetForEditor,
  getDeckRenderHtml as deckGetRenderHtml,
  patchDeck as deckPatch,
} from "./agentParts/deckEditor";
import {
  createShare as shareCreate,
  fetchConversationRun as shareFetchConversationRun,
  fetchShareBundle as shareFetchBundle,
  importShareBundle as shareImportBundle,
} from "./agentParts/share";
import { defaultConversationWsUrl, subscribeLive as liveSubscribe } from "./agentParts/liveSocket";
import { agentFetch, agentGet, agentLive, agentSend, canonicalPreviewBootstrapUrl } from "./client";

// The deck-editor, share-link and live-socket implementations live in
// `./agentParts/*`, but their PUBLIC DECLARATIONS stay here. That is deliberate:
// the public-API authority keys a frontend target on the declaring module and
// records an `export … from` as a re-export, not as a declaration — so moving a
// declaration out, even behind a byte-identical barrel, reads as deleting a
// public target. Frontend function signatures are digested WITHOUT the body
// (`ts_scan.mjs:withoutBody`), so a thin delegator keeps the digest identical.
// Amendment A3 callers routing through an api module also reach the
// preview-bootstrap URL builder from here.
export { canonicalPreviewBootstrapUrl };

export interface DeckPatchResult {
  ok: boolean;
  lowered: LoweredDeck;
  html_file: string;
  pptx_file: string;
  pdf_stale?: boolean;
}

export async function getDeckForEditor(cid: string, base: string): Promise<LoweredDeck> {
  return deckGetForEditor(cid, base);
}

export async function getDeckRenderHtml(
  cid: string,
  base: string,
  template?: string,
): Promise<string> {
  return deckGetRenderHtml(cid, base, template);
}

export async function patchDeck(
  cid: string,
  base: string,
  patch: JsonPatchOp[],
): Promise<DeckPatchResult> {
  return deckPatch(cid, base, patch);
}

export interface ShareLink {
  ok: boolean;
  url?: string;
  token: string;
  conversation_id: string;
  owner_id?: string;
  bundle_seq?: number;
  /** Present when create_share fails (e.g. conversation not found). */
  reason?: string;
}

export interface ShareBundle {
  bundle_version: number;
  conversation_id: string;
  owner_id: string;
  surface: string;
  title: string;
  exported_at: string;
  last_seq: number;
  state: Record<string, unknown>;
  events: Array<Record<string, unknown>>;
  share?: {
    created_at?: string;
    bundle_seq?: number;
    revoked?: boolean;
  };
}

export async function createShare(conversationId: string): Promise<ShareLink> {
  return shareCreate(conversationId);
}

export async function fetchShareBundle(token: string): Promise<ShareBundle | null> {
  return shareFetchBundle(token);
}

export async function importShareBundle(bundle: unknown): Promise<{ conversation_id: string }> {
  return shareImportBundle(bundle);
}

export async function fetchConversationRun(
  cid: string,
): Promise<{ events: Array<Record<string, unknown>>; status: string }> {
  return shareFetchConversationRun(cid);
}

export type ConversationWsUrlFactory = (
  conversationId: string,
  lastSeq: number,
) => string;

export function subscribeLive(
  cid: string,
  onFrame: (f: WSServerFrame) => void,
  wsUrlForCursor: ConversationWsUrlFactory = defaultConversationWsUrl,
): AgentHandle {
  return liveSubscribe(cid, onFrame, wsUrlForCursor);
}


export interface AgentHandle {
  /** Send a client frame (send_message / confirm / reject / cancel). */
  send: (frame: WSClientFrame) => void;
  /** Tear down the subscription. */
  cancel: () => void;
}

/** Create a build-like conversation (the agent loop + the ConfirmRisky gate),
 * optionally pinning the driver model for it (the chat model picker). The surface
 * is "build" (software framing) or "agent" (general-task framing) — identical
 * machinery, so the same create path serves both. */
export async function createBuildConversation(
  modelOverride?: string | null,
  surface: "build" | "agent" = "build",
  autonomous = false,
  /** Weak-model assist tier — EXPLICIT user toggle only (null/false = standard).
   *  Assist is never auto-enabled by hosting; the user opts in via the UI toggle. */
  assist: boolean | null = null,
  quiet = false,
): Promise<string> {
  if (!agentLive()) return FIXTURE_CID;
  const res = await agentSend<{ conversation_id: string }>("POST", "/conversations", {
    surface,
    model_override: modelOverride ?? null,
    autonomous,
    quiet,
    assist,
  });
  return res.conversation_id;
}

/** Apply compose-time settings to a lazily-created upload conversation right
 * before the first kick. The server 409s once real work has started. */
export async function patchConversationSettings(
  conversationId: string,
  settings: {
    modelOverride?: string | null;
    autonomous?: boolean;
    quiet?: boolean;
    assist?: boolean | null;
    depthTier?: "quick" | "standard_deep" | "exhaustive";
    iterative?: boolean;
    recencyWindow?: "month" | "week" | null;
    sources?: string[];
  },
): Promise<void> {
  if (!agentLive()) return;
  await agentSend("PATCH", `/conversations/${conversationId}/settings`, {
    model_override: settings.modelOverride ?? null,
    ...(settings.autonomous !== undefined ? { autonomous: settings.autonomous } : {}),
    ...(settings.quiet !== undefined ? { quiet: settings.quiet } : {}),
    ...(settings.assist !== undefined ? { assist: settings.assist } : {}),
    ...(settings.depthTier !== undefined ? { depth_tier: settings.depthTier } : {}),
    ...(settings.iterative !== undefined ? { iterative: settings.iterative } : {}),
    ...(settings.recencyWindow !== undefined
      ? { recency_window: settings.recencyWindow }
      : {}),
    ...(settings.sources !== undefined ? { sources: settings.sources } : {}),
  });
}

/** The driver-eligible models for the Build chat picker (+ the default). Offline → a
 * small fixture so the picker renders in tests/screenshots. */
export async function listDriverModels(): Promise<DriverModels> {
  if (!agentLive())
    return {
      models: [
        { id: "driver-local", label: "Qwen3.6-27B", provider: "local", free: true, context_window: 131072, capabilities: ["tool_calling"] },
        { id: "driver-overflow", label: "claude-3.5-sonnet", provider: "openrouter", free: false, context_window: 200000, capabilities: ["tool_calling", "vision"] },
      ],
      default: "driver-local",
    };
  return agentGet<DriverModels>("/models");
}

/** P3 — the globally-persisted last-picked driver model from the agent-server.
 * Returns null when no pick has ever been made. Fixture: null (no stored pick
 * offline so the pill falls back to the settings default as before). */
export async function getLastSelectedModel(): Promise<string | null> {
  if (!agentLive()) return null;
  const r = await agentGet<{ model: string | null }>("/models/last-selected");
  return r.model ?? null;
}

/** The backend-aware live preview URL for a conversation's sandbox (or why not). */
export async function getPreview(cid: string): Promise<PreviewInfo> {
  if (!agentLive())
    return { available: false, reason: "Live preview runs against the agent-server (offline here)." };
  return agentGet<PreviewInfo>(`/conversations/${cid}/preview`);
}

export interface WorkspaceVersion {
  seq: number;
  ts: string;
  label?: string | null;
  trigger: "turn" | "finish" | "restore" | string;
  file_count: number;
  total_bytes: number;
  tree_digest: string;
  pinned: boolean;
}

export interface RestoreWorkspaceVersionResult {
  restored: number;
  new_version: number | null;
  tree_digest: string;
}

/** Workspace snapshot history for the Preview pane version picker. Offline → no
 * picker (no false affordance); live uses the agent-server history endpoint. */
export async function listWorkspaceVersions(cid: string): Promise<WorkspaceVersion[]> {
  if (!agentLive()) return [];
  const r = await agentGet<{ versions: WorkspaceVersion[] }>(
    `/conversations/${encodeURIComponent(cid)}/versions`,
  );
  return r.versions ?? [];
}

/** Restore a workspace snapshot. The caller surfaces ApiError.status (409/404/503)
 * with the backend's reason text. */
export async function restoreWorkspaceVersion(
  cid: string,
  seq: number,
): Promise<RestoreWorkspaceVersionResult> {
  return agentSend<RestoreWorkspaceVersionResult>(
    "POST",
    `/conversations/${encodeURIComponent(cid)}/versions/${seq}/restore`,
  );
}

/** Bring a down preview back (§E7): bounded, idempotent restart of the static
 *  serve on the conversation's sandbox. Returns whether a server is now up. */
export async function restartPreview(cid: string): Promise<boolean> {
  if (!agentLive()) return false;
  const r = await agentSend<{ ok: boolean }>("POST", `/conversations/${cid}/preview/restart`);
  return Boolean(r?.ok);
}

export interface UploadResult {
  saved: { name: string; bytes: number }[];
  rejected: { name: string; reason: string }[];
}

/** Upload files into the conversation's sandbox under uploads/ (BP-11). */
export async function uploadFiles(cid: string, files: File[]): Promise<UploadResult> {
  if (!agentLive()) return { saved: [], rejected: [] };
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  const res = await agentFetch(`/conversations/${cid}/files`, {
    method: "POST",
    body: fd,
  });
  const body = (await res.json()) as UploadResult & { detail?: unknown };
  if (!res.ok && res.status !== 413) {
    throw new Error(
      typeof body?.detail === "string"
        ? body.detail
        : `Upload failed (${res.status})`,
    );
  }
  return body as UploadResult;
}

/** The kill switch (BoD §13.6): halt, tear down the sandbox, revoke capabilities. */
export async function killConversation(cid: string): Promise<void> {
  if (!agentLive()) return; // offline: the hook marks the run stopped
  await agentSend("POST", `/conversations/${cid}/kill`);
}

export interface SessionInfo {
  name: string;
  busy: boolean;
  last_line: string;
}

export interface SessionView {
  name: string;
  busy: boolean;
  content: string;
}

/** List live tmux sessions for a conversation's sandbox (BP-14). */
export async function getSessions(cid: string): Promise<{ sessions: SessionInfo[] }> {
  if (!agentLive()) return { sessions: [] };
  return agentGet<{ sessions: SessionInfo[] }>(`/conversations/${cid}/sessions`);
}

/** Capture-pane tail for one named session (BP-14). */
export async function getSessionView(
  cid: string,
  name: string,
  tailChars = 10_000,
): Promise<SessionView> {
  return agentGet<SessionView>(
    `/conversations/${cid}/sessions/${encodeURIComponent(name)}/view?tail_chars=${tailChars}`,
  );
}

/** Resume a PAUSED or interrupted-with-unfinished-plan conversation (BP-12). */
export async function resumeConversation(
  cid: string,
): Promise<{ ok: boolean; status?: string; reason?: string }> {
  if (!agentLive()) return { ok: true, status: "RUNNING" };
  return agentSend<{ ok: boolean; status?: string; reason?: string }>(
    "POST",
    `/conversations/${cid}/resume`,
  );
}

export function subscribeConversation(
  cid: string,
  onFrame: (f: WSServerFrame) => void,
): AgentHandle {
  if (agentLive()) return subscribeLive(cid, onFrame);
  // Dispatch by cid: the Deep Research fixture lives in its own module so the
  // Build fixture stays unmodified.
  if (cid === FIXTURE_DEEP_CID) return subscribeDeepFixture(onFrame);
  return subscribeFixture(cid, onFrame);
}

/** A conversation's sandbox backend name + stored (auto-titled) conversation
 *  title, as read once from the server state endpoint (BP-15 + W-01). The Build
 *  surface's H1 prefers the stored title over the raw first prompt on resume;
 *  the isolation badge reads the sandbox backend name. Amendment A3: this is the
 *  api-layer wrapper so components never import `@/api/client` directly — offline
 *  or a fetch failure both resolve to null (the caller leaves its prior state). */
export async function fetchConversationSummary(
  cid: string,
): Promise<{ sandboxBackend: string | null; title: string | null } | null> {
  if (!agentLive()) return null;
  try {
    const r = await agentFetch(`/conversations/${cid}/state`);
    const s = (await r.json()) as { sandbox_backend?: string; title?: string | null };
    return { sandboxBackend: s.sandbox_backend ?? null, title: s.title ?? null };
  } catch {
    return null;
  }
}

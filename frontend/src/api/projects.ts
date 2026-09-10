/**
 * Build-project data layer — the ONE place that talks to the Projects + storage
 * endpoints. Settings → Project Storage uses the APP server (config DTO); the
 * projects LIST + download + delete go through the AGENT server (the runtime owns
 * the storage path and the live workspace). Same split as sandbox config (app)
 * vs preview proxy (agent).
 *
 * Components NEVER import this file directly — only the project hooks do
 * (data-flow discipline, mirrors the rest of the codebase).
 */

import {
  agentFetch,
  agentGet,
  agentHttpBase,
  agentLive,
  agentSend,
  apiGet,
  apiSend,
  fixtureDelay,
  isLive,
} from "./client";
import { projectDownloadBasename } from "@/lib/downloadFilename";
import type {
  BrowseResult,
  Project,
  ProjectImportInput,
  ProjectImportResult,
  ProjectManifest,
  ProjectsList,
  ProjectStorageConfig,
  ProjectStorageSaveInput,
} from "@/types/project";
import type { ReleaseResponse } from "@/types/release";

// ---- fixture state (offline mode) ------------------------------------------
// A small in-memory mirror so the Projects UI renders + tests run without a
// live backend. Three rows: a normal project, one with files_missing, and a
// fresh one. The path-picker fixture lists a small synthetic tree.

let fixtureStorage: ProjectStorageConfig = {
  projects_root: "/home/user/disco-projects",
  status: "ok",
  // Offline fixture: the user has set a root, so the effective (in-use) root
  // equals the configured one (it only diverges in the zero-config default).
  effective_root: "/home/user/disco-projects",
};

const fixtureProjects: Project[] = [
  {
    id: "conv_demo_snake",
    owner_id: "local",
    title: "Snake game prototype",
    created_at: "2026-06-04T10:23:00Z",
    last_snapshot_at: "2026-06-06T14:11:00Z",
    file_count: 4,
    total_bytes: 8412,
    files_missing: false,
  },
  {
    id: "conv_demo_landing",
    owner_id: "local",
    title: "Landing page redesign",
    created_at: "2026-06-02T09:00:00Z",
    last_snapshot_at: "2026-06-03T18:42:00Z",
    file_count: 7,
    total_bytes: 23104,
    files_missing: false,
  },
  {
    id: "conv_demo_orphan",
    owner_id: "local",
    title: "Deleted-workspace demo",
    created_at: "2026-05-30T08:00:00Z",
    last_snapshot_at: "2026-05-31T12:00:00Z",
    file_count: 0,
    total_bytes: 0,
    files_missing: true,
  },
];

// ---- release assessment fixtures (offline mode) ----------------------------
// One release verdict per demo project, mirroring the WO-7 `/release` wire shape
// EXACTLY (the same key set for every assessment: non-applicable fields are
// `null`/empty, never absent). Covers the three Track-1 assessments so the UI +
// tests exercise all of them with no live backend: `candidate` (self-hostable),
// `needs_review` (ambiguous topology), `not_web` (no web entrypoint). `required_env`
// entries carry NAMES only — never a `value` (secret hygiene).

const RELEASE_COMMAND = "docker compose up -d --build";

const fixtureReleases: Record<string, ReleaseResponse> = {
  // A self-hostable Node web app: validation passed, so `self_host` is true and
  // the download would carry the generated overlay.
  conv_demo_snake: {
    assessment: "candidate",
    reasons: ["A Node web server binding $PORT was detected (server.js)."],
    blockers: [],
    required_env: [
      { name: "PORT", scope: "runtime", required: true, secret: false },
      { name: "SESSION_SECRET", scope: "runtime", required: true, secret: true },
    ],
    command: RELEASE_COMMAND,
    ingress: { service: "web", port: "PORT", health_path: "/healthz" },
    self_host: true,
    spec_digest: "sha256:demo-candidate-0001",
    version_seq: 3,
    tree_digest: "sha256:tree-snake-0001",
  },
  // Ambiguous: more than one plausible entrypoint, so no single ingress resolved.
  // The unresolved release field is reported as a repairable blocker; `ingress`
  // and `spec_digest` are null (no topology bound yet).
  conv_demo_landing: {
    assessment: "needs_review",
    reasons: ["Multiple candidate entrypoints were found; the public ingress is ambiguous."],
    blockers: [
      {
        code: "release_field_unresolved",
        field: "ingress",
        message: "no single web entrypoint could be resolved — declare which service is public.",
        path: null,
      },
    ],
    required_env: [{ name: "API_BASE_URL", scope: "build", required: false, secret: false }],
    command: RELEASE_COMMAND,
    ingress: null,
    self_host: false,
    spec_digest: null,
    version_seq: 5,
    tree_digest: "sha256:tree-landing-0002",
  },
  // Not a web app at all (static docs / script bundle): no ingress, no env, and
  // self-host is off — the Download stays the plain filtered zip (no overlay).
  conv_demo_orphan: {
    assessment: "not_web",
    reasons: ["No web server entrypoint was detected; this looks like a static bundle."],
    blockers: [],
    required_env: [],
    command: RELEASE_COMMAND,
    ingress: null,
    self_host: false,
    spec_digest: null,
    version_seq: 1,
    tree_digest: "sha256:tree-orphan-0003",
  },
};

/** True when `cid` names an offline DEMO project that has a persisted workspace
 * snapshot (`last_snapshot_at` set in the fixture registry). Such a project was
 * committed at a finished state, so RESUMING it offline legitimately rehydrates to a
 * FINISHED view — exactly what a live agent does on resume (restore the committed
 * snapshot). The agent-stream fixture consults this to decide whether a bare resume
 * (no kick) should surface the finished-handoff panels, mirroring the real backend
 * consulting its workspace store. Driven by the registry, never a spec/test marker. */
export function isSnapshottedDemoProject(cid: string): boolean {
  return fixtureProjects.some((p) => p.id === cid && p.last_snapshot_at != null);
}

/** The offline release verdict for `cid`. Unknown projects get the stable
 * `not_web` shape (same key set, empty topology) — never a missing/partial body. */
function fixtureRelease(cid: string): ReleaseResponse {
  const known = fixtureReleases[cid];
  if (known) return { ...known };
  return {
    assessment: "not_web",
    reasons: ["No web server entrypoint was detected."],
    blockers: [],
    required_env: [],
    command: RELEASE_COMMAND,
    ingress: null,
    self_host: false,
    spec_digest: null,
    version_seq: 1,
    tree_digest: `sha256:tree-${cid}`,
  };
}

// ---- projects list / download / delete (AGENT server) ----------------------

export async function listProjects(): Promise<ProjectsList> {
  if (!agentLive()) {
    await fixtureDelay();
    return {
      projects: [...fixtureProjects],
      status: fixtureStorage.projects_root ? "ok" : "unset",
      root: fixtureStorage.projects_root,
    };
  }
  return agentGet<ProjectsList>("/api/projects");
}

/** A SOURCE binding pins a self-host download to the EXACT immutable committed
 * version the operator was shown (WO-C2 §6). BOTH fields are concrete (non-null) by
 * construction — a caller with a nullable release field must narrow it first, so an
 * unsnapshotted/unbound release can never fabricate a binding onto the URL. */
export interface DownloadBinding {
  version_seq: number;
  spec_digest: string;
}

/** The real browser-download mechanism: an anchor navigation to `href`. Used for
 * BOTH the live blob URL (after the authed fetch streams the zip) and the offline
 * bound URL (the browser GETs the pinned zip directly). jsdom no-ops the click; a
 * real browser downloads. Appended before click (some browsers require it). */
function triggerAnchorDownload(href: string, filename: string): void {
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Trigger a browser download of the project's workspace zip. When `binding` is
 * supplied the download is SOURCE-BOUND: its query carries the exact
 * (`version_seq`, `spec_digest`) the release verdict named, so the byte stream is the
 * immutable stored version — never a drifting live mirror (WO-C2 §6). A `null`
 * binding requests the plain, unbound zip and fabricates NO query. `title` only
 * names the saved FILE (never the request), so a missing title degrades to the
 * conversation id and nothing else changes. Returns nothing on success; throws on
 * a server error (e.g. files_missing → 404). */
export async function downloadProject(
  cid: string,
  binding: DownloadBinding | null,
  title?: string | null,
): Promise<void> {
  // UI-40: name the file after the project, not the opaque conversation id.
  const filename = `${projectDownloadBasename(cid, title)}.zip`;
  // Real URL-encoding (URLSearchParams → `:` becomes `%3A`), not a raw splice.
  const query = binding
    ? `?${new URLSearchParams({
        version_seq: String(binding.version_seq),
        spec_digest: binding.spec_digest,
      }).toString()}`
    : "";
  const url = `${agentHttpBase()}/api/projects/${encodeURIComponent(cid)}/download${query}`;
  if (!agentLive()) {
    await fixtureDelay();
    // Offline/demo fidelity: a BOUND download still issues a real, browser-observable
    // GET to the bound URL so the operator's browser downloads the pinned zip (the
    // fixture server 404s the body — the binding riding the query is the point). An
    // UNBOUND offline download has no live workspace to stream, so it stays a no-op.
    if (binding) triggerAnchorDownload(url, filename);
    return;
  }
  const res = await agentFetch(url, { headers: { accept: "application/zip" } });
  if (!res.ok) {
    let reason = `${res.status}`;
    try {
      const body = await res.json();
      reason = body?.detail?.reason ?? reason;
    } catch {
      /* opaque; keep status */
    }
    throw new Error(`download failed: ${reason}`);
  }
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  triggerAnchorDownload(objectUrl, filename);
  URL.revokeObjectURL(objectUrl);
}

/** Fetch + download the project's manifest JSON (files + deliverable metadata). */
export async function exportProjectManifest(cid: string): Promise<void> {
  if (!agentLive()) {
    await fixtureDelay();
    return;
  }
  const url = `${agentHttpBase()}/api/projects/${encodeURIComponent(cid)}/manifest`;
  const res = await agentFetch(url, { headers: { accept: "application/json" } });
  if (!res.ok) {
    let reason = `${res.status}`;
    try {
      const body = await res.json();
      reason = body?.detail?.reason ?? reason;
    } catch {
      /* opaque; keep status */
    }
    throw new Error(`manifest export failed: ${reason}`);
  }
  const text = JSON.stringify(await res.json(), null, 2);
  const blob = new Blob([text], { type: "application/json" });
  const objectUrl = URL.createObjectURL(blob);
  triggerAnchorDownload(objectUrl, `${cid}-manifest.json`);
  URL.revokeObjectURL(objectUrl);
}

export async function getProjectManifest(cid: string): Promise<ProjectManifest> {
  if (!agentLive()) {
    await fixtureDelay();
    return {
      conversation_id: cid,
      title: cid,
      created_at: null,
      last_snapshot_at: null,
      file_count: 0,
      total_bytes: 0,
      files: [],
      deliverable: null,
    };
  }
  return agentGet<ProjectManifest>(`/api/projects/${encodeURIComponent(cid)}/manifest`);
}

/** Assess whether a project's committed workspace can be self-hosted (WO-7).
 * Live: `GET /api/projects/{cid}/release` on the AGENT server (the runtime owns
 * the workspace + release intent). Offline: the in-repo fixture verdict, so the
 * capabilities UI renders + tests run with no backend. NAMES only — never secret
 * values (the response type has no `value` field). */
export async function getProjectRelease(cid: string): Promise<ReleaseResponse> {
  if (!agentLive()) {
    await fixtureDelay();
    return fixtureRelease(cid);
  }
  return agentGet<ReleaseResponse>(`/api/projects/${encodeURIComponent(cid)}/release`);
}

export async function deleteProject(cid: string): Promise<{ id: string }> {
  if (!agentLive()) {
    await fixtureDelay();
    const idx = fixtureProjects.findIndex((p) => p.id === cid);
    if (idx >= 0) fixtureProjects.splice(idx, 1);
    return { id: cid };
  }
  await agentSend("DELETE", `/api/projects/${encodeURIComponent(cid)}`);
  return { id: cid };
}

function importErrorMessage(body: unknown, fallback: string): string {
  if (typeof body !== "object" || body === null) return fallback;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (typeof detail !== "object" || detail === null) return fallback;
  const reason = (detail as { reason?: unknown }).reason;
  const message = (detail as { message?: unknown }).message;
  if (typeof message === "string" && typeof reason === "string") return `${reason}: ${message}`;
  if (typeof message === "string") return message;
  if (typeof reason === "string") return reason;
  return fallback;
}

async function parseImportResponse(res: Response): Promise<ProjectImportResult> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    throw new Error(importErrorMessage(body, `Import failed (${res.status})`));
  }
  return body as ProjectImportResult;
}

export async function importProject(input: ProjectImportInput): Promise<ProjectImportResult> {
  if (!agentLive()) {
    await fixtureDelay();
    throw new Error("Project import requires a live agent server.");
  }
  const url = `${agentHttpBase()}/api/projects/import`;
  if (input.kind === "zip") {
    const fd = new FormData();
    fd.append("file", input.file);
    return parseImportResponse(await agentFetch(url, { method: "POST", body: fd }));
  }
  const body =
    input.kind === "path"
      ? { path: input.path }
      : { git_url: input.gitUrl };
  return parseImportResponse(
    await agentFetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

// ---- storage path config (APP server) --------------------------------------

export async function getProjectsConfig(): Promise<ProjectStorageConfig> {
  if (!isLive()) {
    await fixtureDelay();
    return { ...fixtureStorage };
  }
  return apiGet<ProjectStorageConfig>("/api/projects/storage/config");
}

export async function updateProjectsConfig(
  cfg: ProjectStorageSaveInput,
): Promise<ProjectStorageConfig> {
  if (!isLive()) {
    await fixtureDelay();
    // Offline: mirror the server's derivation — status from validity, and the
    // effective root equals the configured one (no auto-default to compute here).
    fixtureStorage = {
      ...cfg,
      status: cfg.projects_root ? "ok" : "unset",
      effective_root: cfg.projects_root,
    };
    return { ...fixtureStorage };
  }
  return apiSend<ProjectStorageConfig>("PUT", "/api/projects/storage/config", cfg);
}

// ---- server-side directory picker (AGENT server) ---------------------------

const FIXTURE_TREE: Record<string, BrowseResult> = {
  "/home/user": {
    path: "/home/user",
    parent: "/home",
    selectable: "ok",
    entries: [
      { name: "disco-projects", is_dir: true },
      { name: "Documents", is_dir: true },
      { name: "Downloads", is_dir: true },
      { name: "code", is_dir: true },
      { name: "notes.md", is_dir: false },
    ],
  },
  "/home/user/disco-projects": {
    path: "/home/user/disco-projects",
    parent: "/home/user",
    selectable: "ok",
    entries: [
      { name: "conv_demo_snake", is_dir: true },
      { name: "conv_demo_landing", is_dir: true },
    ],
  },
};

export async function browseStorage(path: string): Promise<BrowseResult> {
  if (!agentLive()) {
    await fixtureDelay();
    const target = path || "/home/user";
    return FIXTURE_TREE[target] ?? FIXTURE_TREE["/home/user"];
  }
  const params = new URLSearchParams(path ? { path } : {});
  return agentGet<BrowseResult>(`/api/storage/browse?${params.toString()}`);
}

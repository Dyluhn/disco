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
  agentGet,
  agentHttpBase,
  agentLive,
  agentSend,
  apiGet,
  apiSend,
  fixtureDelay,
  isLive,
} from "./client";
import type {
  BrowseResult,
  Project,
  ProjectsList,
  ProjectStorageConfig,
} from "@/types/project";

const OWNER_ID = (import.meta.env.VITE_OWNER_ID as string | undefined) ?? "local";

// ---- fixture state (offline mode) ------------------------------------------
// A small in-memory mirror so the Projects UI renders + tests run without a
// live backend. Three rows: a normal project, one with files_missing, and a
// fresh one. The path-picker fixture lists a small synthetic tree.

let fixtureStorage: ProjectStorageConfig = {
  projects_root: "/home/dylan/disco-projects",
  status: "ok",
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
  const params = new URLSearchParams({ owner_id: OWNER_ID });
  return agentGet<ProjectsList>(`/api/projects?${params.toString()}`);
}

/** Trigger a browser download of the project's workspace zip. Returns nothing
 * on success; throws on a server error (e.g. files_missing → 404). */
export async function downloadProject(cid: string): Promise<void> {
  if (!agentLive()) {
    await fixtureDelay();
    return;
  }
  const url = `${agentHttpBase()}/api/projects/${encodeURIComponent(cid)}/download`;
  const res = await fetch(url, { headers: { accept: "application/zip" } });
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
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = `${cid}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(objectUrl);
}

/** Fetch + download the project's manifest JSON (files + deliverable metadata). */
export async function exportProjectManifest(cid: string): Promise<void> {
  if (!agentLive()) {
    await fixtureDelay();
    return;
  }
  const url = `${agentHttpBase()}/api/projects/${encodeURIComponent(cid)}/manifest`;
  const res = await fetch(url, { headers: { accept: "application/json" } });
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
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = `${cid}-manifest.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(objectUrl);
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

// ---- storage path config (APP server) --------------------------------------

export async function getProjectsConfig(): Promise<ProjectStorageConfig> {
  if (!isLive()) {
    await fixtureDelay();
    return { ...fixtureStorage };
  }
  return apiGet<ProjectStorageConfig>("/api/projects/storage/config");
}

export async function updateProjectsConfig(
  cfg: ProjectStorageConfig,
): Promise<ProjectStorageConfig> {
  if (!isLive()) {
    await fixtureDelay();
    fixtureStorage = { ...cfg, status: cfg.projects_root ? "ok" : "unset" };
    return { ...fixtureStorage };
  }
  return apiSend<ProjectStorageConfig>("PUT", "/api/projects/storage/config", cfg);
}

// ---- server-side directory picker (AGENT server) ---------------------------

const FIXTURE_TREE: Record<string, BrowseResult> = {
  "/home/dylan": {
    path: "/home/dylan",
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
  "/home/dylan/disco-projects": {
    path: "/home/dylan/disco-projects",
    parent: "/home/dylan",
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
    const target = path || "/home/dylan";
    return FIXTURE_TREE[target] ?? FIXTURE_TREE["/home/dylan"];
  }
  const params = new URLSearchParams(path ? { path } : {});
  return agentGet<BrowseResult>(`/api/storage/browse?${params.toString()}`);
}

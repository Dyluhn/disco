import { DEFAULT_ASSIGNMENTS, MODEL_CATALOGUE } from "@/fixtures/models";
import { ApiError } from "./client";
import type { AssignmentsPatch, ModelAssignments, ModelInfo, ModelUpsert } from "@/types/models";
import { apiGet, apiSend, fixtureDelay, isLive } from "./client";

/**
 * Data-access layer for the model catalogue + assignments (the absolute, manual
 * model story). Components NEVER call this directly — only hooks (useModels,
 * useAssignments, useUpdateAssignments) do, per the data-flow discipline.
 *
 * Live (VITE_API_BASE set) → the app-server endpoints GET /api/models,
 * GET/PUT /api/models/assignments (api-endpoints.md). Otherwise → the in-repo
 * fixture (a session-scoped in-memory copy; no browser storage). The DTO shapes
 * are identical to the frontend types, so live JSON maps straight through.
 */

// In-memory, session-scoped state for the FIXTURE path (deep copies so mutations
// don't leak into the fixture constants).
let fixtureAssignments: ModelAssignments = {
  default_model: DEFAULT_ASSIGNMENTS.default_model,
  roles: { ...DEFAULT_ASSIGNMENTS.roles },
};
let fixtureCatalogue: ModelInfo[] = MODEL_CATALOGUE.map((m) => ({ ...m }));

/** Derive the display fields for a fixture-path upsert, mirroring the backend. */
function toModelInfo(u: ModelUpsert): ModelInfo {
  const humanized = u.id.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const name = u.model_id.split("/").pop()!.replace(/\.gguf$/, "");
  const paid = u.price_in_per_m > 0 || u.price_out_per_m > 0;
  const ctx = u.context_window >= 1000 ? `${Math.floor(u.context_window / 1000)}K ctx` : `${u.context_window} ctx`;
  const host = u.base_url ? u.base_url.replace(/^https?:\/\//, "").split("/")[0] : null;
  const note = [ctx, u.quantization, host].filter(Boolean).join(" · ");
  return {
    id: u.id,
    label: `${humanized} — ${name}`,
    provider: paid ? "openrouter" : "local",
    price_in_per_m: u.price_in_per_m,
    price_out_per_m: u.price_out_per_m,
    capabilities: [...u.capabilities],
    note,
    model_id: u.model_id,
    base_url: u.base_url ?? null,
    api_key_env: u.api_key_env ?? null,
    context_window: u.context_window,
    quantization: u.quantization ?? null,
  };
}

export async function listModels(): Promise<ModelInfo[]> {
  if (isLive()) return apiGet<ModelInfo[]>("/api/models");
  await fixtureDelay();
  return fixtureCatalogue.map((m) => ({ ...m }));
}

/** Add a model to the catalogue. */
export async function createModel(upsert: ModelUpsert): Promise<ModelInfo[]> {
  if (isLive()) return apiSend<ModelInfo[]>("POST", "/api/models", upsert);
  await fixtureDelay();
  if (fixtureCatalogue.some((m) => m.id === upsert.id)) {
    throw new ApiError(`model ${upsert.id} already exists`, 400);
  }
  fixtureCatalogue = [...fixtureCatalogue, toModelInfo(upsert)];
  return fixtureCatalogue.map((m) => ({ ...m }));
}

/** Edit an existing catalogue model (id is immutable). */
export async function updateModel(id: string, upsert: ModelUpsert): Promise<ModelInfo[]> {
  if (isLive()) return apiSend<ModelInfo[]>("PUT", `/api/models/${encodeURIComponent(id)}`, upsert);
  await fixtureDelay();
  if (!fixtureCatalogue.some((m) => m.id === id)) throw new ApiError(`unknown model ${id}`, 400);
  fixtureCatalogue = fixtureCatalogue.map((m) => (m.id === id ? toModelInfo(upsert) : m));
  return fixtureCatalogue.map((m) => ({ ...m }));
}

/** Remove a model — refused if it's the default or assigned to a role. */
export async function deleteModel(id: string): Promise<ModelInfo[]> {
  if (isLive()) return apiSend<ModelInfo[]>("DELETE", `/api/models/${encodeURIComponent(id)}`);
  await fixtureDelay();
  if (id === fixtureAssignments.default_model) {
    throw new ApiError(`${id} is the default model — reassign the default first`, 400);
  }
  const role = Object.entries(fixtureAssignments.roles).find(([, k]) => k === id)?.[0];
  if (role) throw new ApiError(`${id} is assigned to ${role} — reassign first`, 400);
  fixtureCatalogue = fixtureCatalogue.filter((m) => m.id !== id);
  return fixtureCatalogue.map((m) => ({ ...m }));
}

export async function getAssignments(): Promise<ModelAssignments> {
  if (isLive()) return apiGet<ModelAssignments>("/api/models/assignments");
  await fixtureDelay();
  return {
    default_model: fixtureAssignments.default_model,
    roles: { ...fixtureAssignments.roles },
  };
}

/**
 * Apply a partial assignment change. Assignments are ABSOLUTE — the system uses
 * exactly what is set; there is no validation/prediction here (capabilities are
 * advisory + fail-loud at runtime, not blocked in the UI).
 */
export async function updateAssignments(patch: AssignmentsPatch): Promise<ModelAssignments> {
  if (isLive()) return apiSend<ModelAssignments>("PUT", "/api/models/assignments", patch);
  await fixtureDelay();
  fixtureAssignments = {
    default_model: patch.default_model ?? fixtureAssignments.default_model,
    roles: { ...fixtureAssignments.roles, ...(patch.roles ?? {}) },
  };
  return {
    default_model: fixtureAssignments.default_model,
    roles: { ...fixtureAssignments.roles },
  };
}

/**
 * Model-catalogue CRUD + role assignments — the fixture-vs-live data path for
 * `listModels`/`createModel`/`updateModel`/`deleteModel`/`getAssignments`/
 * `updateAssignments`.
 *
 * Extracted from api/models.ts (module-size decomposition, TS-0003);
 * re-exported (as thin delegators) from api/models so the public import path
 * and every signature stay unchanged.
 *
 * `fixtureCatalogueSnapshot`/`appendFixtureCatalogueEntry` and `toModelInfo`
 * are also used by `./providers` (provider-enabled models land in the SAME
 * fixture catalogue this module owns), so they are exported here rather than
 * kept module-private.
 */

import { DEFAULT_ASSIGNMENTS, MODEL_CATALOGUE } from "@/fixtures/models";
import { ApiError } from "../client";
import type { AssignmentsPatch, ModelAssignments, ModelInfo, ModelUpsert } from "@/types/models";
import { apiGet, apiSend, fixtureDelay, isLive } from "../client";

// In-memory, session-scoped state for the FIXTURE path (deep copies so mutations
// don't leak into the fixture constants).
let fixtureAssignments: ModelAssignments = {
  default_model: DEFAULT_ASSIGNMENTS.default_model,
  vision_model: DEFAULT_ASSIGNMENTS.vision_model,
  roles: { ...DEFAULT_ASSIGNMENTS.roles },
};
let fixtureCatalogue: ModelInfo[] = MODEL_CATALOGUE.map((m) => ({ ...m }));

function visionStatus(u: ModelUpsert): ModelInfo["vision_status"] {
  if (u.vision === true || u.capabilities.includes("vision")) return "vision";
  if (u.vision === false) return "text-only";
  return "unknown";
}

/** Derive the display fields for a fixture-path upsert, mirroring the backend. */
export function toModelInfo(u: ModelUpsert): ModelInfo {
  // W-04: drop the `or-` OpenRouter prefix BEFORE humanizing so the label doesn't
  // gain a bogus "Or " word (mirrors the backend mapper's _display_key).
  const humanized = u.id
    .replace(/^or-/, "")
    .replace(/-/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
  const name = u.model_id.split("/").pop()!.replace(/\.gguf$/, "");
  const paid = u.price_in_per_m > 0 || u.price_out_per_m > 0;
  const ctx = u.context_window >= 1000 ? `${Math.floor(u.context_window / 1000)}K ctx` : `${u.context_window} ctx`;
  const host = u.base_url ? u.base_url.replace(/^https?:\/\//, "").split("/")[0] : null;
  const note = [ctx, u.quantization, host].filter(Boolean).join(" · ");
  return {
    id: u.id,
    request_policy: u.request_policy,
    label: `${humanized} — ${name}`,
    provider: paid ? "openrouter" : "local",
    price_in_per_m: u.price_in_per_m,
    price_out_per_m: u.price_out_per_m,
    // W-05: derive when unset (subscription must be explicit — it has a 0 per-token
    // price, so it can't be inferred); carry an explicit mode through verbatim.
    pricing_mode: u.pricing_mode ?? (paid ? "metered" : "free"),
    capabilities: [...u.capabilities],
    vision: u.vision ?? null,
    vision_status: visionStatus(u),
    note,
    model_id: u.model_id,
    base_url: u.base_url ?? null,
    api_key_env: u.api_key_env ?? null,
    requires_api_key: u.requires_api_key ?? true,
    context_window: u.context_window,
    max_output_tokens: u.max_output_tokens ?? null,
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
  if (id === fixtureAssignments.vision_model) {
    throw new ApiError(`${id} is the vision model — reassign vision first`, 400);
  }
  fixtureCatalogue = fixtureCatalogue.filter((m) => m.id !== id);
  return fixtureCatalogue.map((m) => ({ ...m }));
}

export async function getAssignments(): Promise<ModelAssignments> {
  if (isLive()) return apiGet<ModelAssignments>("/api/models/assignments");
  await fixtureDelay();
  return {
    default_model: fixtureAssignments.default_model,
    vision_model: fixtureAssignments.vision_model,
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
    vision_model: Object.prototype.hasOwnProperty.call(patch, "vision_model")
      ? (patch.vision_model ?? null)
      : fixtureAssignments.vision_model,
    roles: { ...fixtureAssignments.roles, ...(patch.roles ?? {}) },
  };
  return {
    default_model: fixtureAssignments.default_model,
    vision_model: fixtureAssignments.vision_model,
    roles: { ...fixtureAssignments.roles },
  };
}

// ---- shared catalogue accessors (consumed by ./providers) -----------------

/** Read-only view of the live fixture catalogue array. Not a defensive copy —
 * for lookups only (`.find`/`.some`/`.filter`); mutation stays funneled through
 * `appendFixtureCatalogueEntry` so this module is the sole writer. */
export function fixtureCatalogueSnapshot(): ModelInfo[] {
  return fixtureCatalogue;
}

/** Append an already-built catalogue entry and return a copy of the new state,
 * mirroring the tail of `createModel`/`enableProviderModel`'s fixture path. */
export function appendFixtureCatalogueEntry(entry: ModelInfo): ModelInfo[] {
  fixtureCatalogue = [...fixtureCatalogue, entry];
  return fixtureCatalogue.map((m) => ({ ...m }));
}

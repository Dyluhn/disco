import { DEFAULT_ASSIGNMENTS, MODEL_CATALOGUE } from "@/fixtures/models";
import type { AssignmentsPatch, ModelAssignments, ModelInfo } from "@/types/models";
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

// In-memory, session-scoped assignment state for the FIXTURE path (deep copy so
// mutations don't leak into the fixture constant).
let fixtureAssignments: ModelAssignments = {
  default_model: DEFAULT_ASSIGNMENTS.default_model,
  roles: { ...DEFAULT_ASSIGNMENTS.roles },
};

export async function listModels(): Promise<ModelInfo[]> {
  if (isLive()) return apiGet<ModelInfo[]>("/api/models");
  await fixtureDelay();
  return MODEL_CATALOGUE;
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

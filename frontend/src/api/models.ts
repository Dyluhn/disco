import { DEFAULT_ASSIGNMENTS, MODEL_CATALOGUE } from "@/fixtures/models";
import type { AssignmentsPatch, ModelAssignments, ModelInfo } from "@/types/models";

/**
 * Data-access layer for the model catalogue + assignments (the absolute, manual
 * model story). Components NEVER call this directly — only hooks (useModels,
 * useAssignments, useUpdateAssignments) do, per the data-flow discipline.
 *
 * Fixture-backed for now. The session-scoped assignment store is a module-level
 * copy (no browser storage). Swap these bodies for the live router-config
 * endpoints (GET /api/models, GET/PUT /api/models/assignments) when wired; the
 * shapes are identical.
 */

const FAKE_LATENCY_MS = 20;
const delay = () => new Promise((r) => setTimeout(r, FAKE_LATENCY_MS));

// In-memory, session-scoped assignment state (deep copy so mutations don't leak
// into the fixture constant).
let assignments: ModelAssignments = {
  default_model: DEFAULT_ASSIGNMENTS.default_model,
  roles: { ...DEFAULT_ASSIGNMENTS.roles },
};

export async function listModels(): Promise<ModelInfo[]> {
  await delay();
  return MODEL_CATALOGUE;
}

export async function getAssignments(): Promise<ModelAssignments> {
  await delay();
  return { default_model: assignments.default_model, roles: { ...assignments.roles } };
}

/**
 * Apply a partial assignment change. Assignments are ABSOLUTE — the system uses
 * exactly what is set; there is no validation/prediction here (capabilities are
 * advisory + fail-loud at runtime, not blocked in the UI).
 */
export async function updateAssignments(patch: AssignmentsPatch): Promise<ModelAssignments> {
  await delay();
  assignments = {
    default_model: patch.default_model ?? assignments.default_model,
    roles: { ...assignments.roles, ...(patch.roles ?? {}) },
  };
  return { default_model: assignments.default_model, roles: { ...assignments.roles } };
}

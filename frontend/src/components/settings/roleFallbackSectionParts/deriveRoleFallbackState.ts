/**
 * Pure derived-state helpers for RoleFallbackSection — pulled out of the
 * component purely to shed cyclomatic complexity (TS-0034). No hooks, no
 * transport.
 */

import type { RoleFallbackConfig } from "@/types/models";

/** True when the draft (enabled/baseUrl/model/apiKeyEnv) has diverged from
 * the persisted config. */
export function isRoleFallbackDirty(
  data: RoleFallbackConfig | undefined,
  enabled: boolean,
  baseUrl: string,
  model: string,
  apiKeyEnv: string,
): boolean {
  return (
    !!data &&
    (enabled !== data.enabled ||
      baseUrl !== data.base_url ||
      model !== data.model ||
      apiKeyEnv !== data.api_key_env)
  );
}

/** An enabled fallback needs both a base URL and a model before it can save. */
export function isRoleFallbackIncomplete(
  enabled: boolean,
  baseUrl: string,
  model: string,
): boolean {
  return enabled && (!baseUrl.trim() || !model.trim());
}

export function canSaveRoleFallback(
  dirty: boolean,
  incomplete: boolean,
  pending: boolean,
): boolean {
  return dirty && !incomplete && !pending;
}

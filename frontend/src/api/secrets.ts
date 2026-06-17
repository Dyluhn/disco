/**
 * Generic provider secrets — any API key, encrypted at rest by name.
 *
 * Mirrors the OpenRouter key client but keyed by env-var NAME, so the user can
 * store the key for any paid model / search / extraction / TTS provider. Values
 * are write-only: the API never returns a stored key, only whether one exists.
 */
import { ApiError } from "./client";
import { apiGet, apiSend, fixtureDelay, isLive } from "./client";

export interface SecretsList {
  names: string[];
  locked: boolean;
  can_store: boolean;
}

export interface SecretStatus {
  name: string;
  configured: boolean;
  locked: boolean;
  can_store: boolean;
}

// ---- offline fixture (so Settings renders + tests run without a backend) ----
let fixtureNames = new Set<string>();
const fixtureCanStore = true;

export async function listSecrets(): Promise<SecretsList> {
  if (isLive()) return apiGet<SecretsList>("/api/secrets");
  await fixtureDelay();
  return { names: [...fixtureNames].sort(), locked: false, can_store: fixtureCanStore };
}

export async function setSecret(name: string, value: string): Promise<SecretStatus> {
  if (isLive())
    return apiSend<SecretStatus>("PUT", `/api/secrets/${encodeURIComponent(name)}`, { value });
  await fixtureDelay();
  if (!name.trim()) throw new ApiError("name is empty", 400);
  if (!value.trim()) throw new ApiError("value is empty", 400);
  if (name === "openrouter") throw new ApiError("use the OpenRouter section for that key", 400);
  fixtureNames.add(name);
  return { name, configured: true, locked: false, can_store: fixtureCanStore };
}

export async function clearSecret(name: string): Promise<SecretStatus> {
  if (isLive())
    return apiSend<SecretStatus>("DELETE", `/api/secrets/${encodeURIComponent(name)}`);
  await fixtureDelay();
  fixtureNames.delete(name);
  return { name, configured: false, locked: false, can_store: fixtureCanStore };
}

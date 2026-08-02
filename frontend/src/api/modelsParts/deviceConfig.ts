/**
 * Device/agent-server backend config: sandbox, encoders, TTS, image-gen, data
 * sources, role fallback — plus the T4.2/T4.3/T4.4 live provider probes.
 *
 * Extracted from api/models.ts (module-size decomposition, TS-0003);
 * re-exported (as thin delegators) from api/models so the public import path
 * and every signature stay unchanged.
 */

import { ApiError } from "../client";
import type {
  DataSourcesConfig,
  EncodersConfig,
  ImageGenConfig,
  RoleFallbackConfig,
  TtsConfig,
} from "@/types/models";
import type { SandboxConfig, SandboxHealth } from "@/types/sandbox";
import type { ProbeResult } from "@/types/probe";
import { agentGet, agentLive, agentSend, apiGet, apiSend, fixtureDelay, isLive } from "../client";

// ---- sandbox backend -------------------------------------------------------

let fixtureSandbox: SandboxConfig = {
  backend: "local",
  docker_socket: "unix:///var/run/docker.sock",
  podman_url: "unix:///run/user/1000/podman/podman.sock",
  runtime: "runc",
  image: "disco-sandbox:base",
  workspace_root: "/var/lib/disco/workspaces",
};

export async function getSandboxConfig(): Promise<SandboxConfig> {
  if (isLive()) return apiGet<SandboxConfig>("/api/sandbox/config");
  await fixtureDelay();
  return { ...fixtureSandbox };
}

export async function updateSandboxConfig(cfg: SandboxConfig): Promise<SandboxConfig> {
  if (isLive()) return apiSend<SandboxConfig>("PUT", "/api/sandbox/config", cfg);
  await fixtureDelay();
  fixtureSandbox = { ...cfg };
  return { ...fixtureSandbox };
}

/** W-48 — connectivity preflight for a sandbox backend. Probed on the AGENT-server,
 * which owns the sandbox environment (the app-server has no container socket in a
 * split-container deploy, so its probe would falsely fail). Real, bounded probe →
 * a typed host-naming verdict (never throws for an expected failure). The fixture
 * path returns a synthetic "reachable". */
export async function testSandbox(cfg: SandboxConfig): Promise<ProbeResult> {
  if (agentLive()) return agentSend<ProbeResult>("POST", "/api/sandbox/test", cfg);
  await fixtureDelay();
  return { ok: true, status: "ok", detail: `${cfg.backend} sandbox is reachable.` };
}

/** Reachability of the ACTIVE (persisted) sandbox backend — the cheap signal the app
 * shell polls to surface an unreachable sandbox BEFORE a doomed run. Probed on the
 * AGENT-server (which owns the sandbox environment), against the SAME service the run
 * path builds, so the banner and a real run agree. The fixture path returns a synthetic
 * "reachable" (dev = process backend). */
export async function getSandboxHealth(): Promise<SandboxHealth> {
  if (agentLive()) return agentGet<SandboxHealth>("/api/sandbox/health");
  await fixtureDelay();
  return { reachable: true, backend: fixtureSandbox.backend, detail: "" };
}

// ---- encoders (bundled-local vs remote) ------------------------------------

let fixtureEncoders: EncodersConfig = { remote: false };

export async function getEncodersConfig(): Promise<EncodersConfig> {
  if (isLive()) return apiGet<EncodersConfig>("/api/encoders/config");
  await fixtureDelay();
  return { ...fixtureEncoders };
}

export async function updateEncodersConfig(cfg: EncodersConfig): Promise<EncodersConfig> {
  if (isLive()) return apiSend<EncodersConfig>("PUT", "/api/encoders/config", cfg);
  await fixtureDelay();
  fixtureEncoders = { ...cfg };
  return { ...fixtureEncoders };
}

// ---- TTS (audio-overview toggle / bundled-vs-remote / voices) --------------

let fixtureTts: TtsConfig = {
  enabled: true,
  provider: "bundled",
  base_url: "",
  api_key_env: "",
  model: "",
  voice_a: "af_heart",
  voice_b: "af_bella",
};

export async function getTtsConfig(): Promise<TtsConfig> {
  if (isLive()) return apiGet<TtsConfig>("/api/tts/config");
  await fixtureDelay();
  return { ...fixtureTts };
}

export async function updateTtsConfig(cfg: TtsConfig): Promise<TtsConfig> {
  if (isLive()) return apiSend<TtsConfig>("PUT", "/api/tts/config", cfg);
  await fixtureDelay();
  fixtureTts = { ...cfg };
  return { ...fixtureTts };
}

// ---- image generation (comfyui / openai / openrouter provider tiers) -------

let fixtureImageGen: ImageGenConfig = {
  provider: "openrouter",
  base_url: "",
  api_key_env: "",
  model: "",
};

export async function getImageGenConfig(): Promise<ImageGenConfig> {
  if (isLive()) return apiGet<ImageGenConfig>("/api/image-gen/config");
  await fixtureDelay();
  return { ...fixtureImageGen };
}

export async function updateImageGenConfig(cfg: ImageGenConfig): Promise<ImageGenConfig> {
  if (isLive()) return apiSend<ImageGenConfig>("PUT", "/api/image-gen/config", cfg);
  await fixtureDelay();
  fixtureImageGen = { ...cfg };
  return { ...fixtureImageGen };
}

// ---- data sources (search + extraction provider tiers) ---------------------

let fixtureDataSources: DataSourcesConfig = {
  search_provider: "ddgs",
  search_base_url: "",
  search_api_key_env: "",
  extraction_provider: "local",
  extraction_base_url: "",
  extraction_api_key_env: "",
  configured_sources: [],
};

export async function getDataSourcesConfig(): Promise<DataSourcesConfig> {
  if (isLive()) return apiGet<DataSourcesConfig>("/api/data-sources/config");
  await fixtureDelay();
  return { ...fixtureDataSources };
}

export async function updateDataSourcesConfig(cfg: DataSourcesConfig): Promise<DataSourcesConfig> {
  if (isLive()) return apiSend<DataSourcesConfig>("PUT", "/api/data-sources/config", cfg);
  await fixtureDelay();
  fixtureDataSources = { ...cfg };
  return { ...fixtureDataSources };
}

// ---- role fallback (auxiliary role resilience) ----------------------------

let fixtureRoleFallback: RoleFallbackConfig = {
  enabled: false,
  base_url: "",
  model: "",
  api_key_env: "",
};

export async function getRoleFallbackConfig(): Promise<RoleFallbackConfig> {
  if (isLive()) return apiGet<RoleFallbackConfig>("/api/role-fallback/config");
  await fixtureDelay();
  return { ...fixtureRoleFallback };
}

export async function updateRoleFallbackConfig(
  cfg: RoleFallbackConfig,
): Promise<RoleFallbackConfig> {
  if (isLive()) return apiSend<RoleFallbackConfig>("PUT", "/api/role-fallback/config", cfg);
  await fixtureDelay();
  if (cfg.enabled && (!cfg.base_url.trim() || !cfg.model.trim())) {
    throw new ApiError("Role fallback needs both a base URL and model when enabled.", 400);
  }
  fixtureRoleFallback = { ...cfg };
  return { ...fixtureRoleFallback };
}

// ---- T4.2/T4.3/T4.4 live provider probes -----------------------------------

/** T4.2 — reachability of the configured search/extraction endpoint (app-server). */
export async function testDataSource(kind: "search" | "extraction"): Promise<ProbeResult> {
  return apiSend<ProbeResult>("POST", `/api/data-sources/${kind}/test`);
}

/** T4.3 — synthesize a one-word clip via the configured TTS tier (agent-server). */
export async function testTts(): Promise<ProbeResult> {
  return agentSend<ProbeResult>("POST", "/api/tts/test");
}

/** T4.4 — one tiny generation via the configured image tier; reports NOT-CONFIGURED
 * honestly when no real backend is set up (W-50) (agent-server). */
export async function testImageGen(): Promise<ProbeResult> {
  return agentSend<ProbeResult>("POST", "/api/image-gen/test");
}

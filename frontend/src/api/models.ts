import type {
  AssignmentsPatch,
  BuildKernelConfig,
  DataSourcesConfig,
  EncodersConfig,
  ImageGenConfig,
  LiveBrowserConfig,
  ModelAssignments,
  ModelInfo,
  ModelUpsert,
  OpenRouterKeyStatus,
  OpenRouterModel,
  ProviderCatalogueModel,
  ProviderCreate,
  ProviderEnableBody,
  ProviderInfo,
  ProviderMutationResult,
  ProviderPatch,
  ProviderPreset,
  RoleFallbackConfig,
  TtsConfig,
} from "@/types/models";
import type { SandboxConfig, SandboxHealth } from "@/types/sandbox";
import type { ProbeResult } from "@/types/probe";
import * as crud from "./modelsParts/crud";
import * as deviceConfig from "./modelsParts/deviceConfig";
import * as openRouter from "./modelsParts/openRouter";
import * as providers from "./modelsParts/providers";
import * as platformConfig from "./modelsParts/platformConfig";

/**
 * Data-access layer for the model catalogue + assignments (the absolute, manual
 * model story). Components NEVER call this directly — only hooks (useModels,
 * useAssignments, useUpdateAssignments) do, per the data-flow discipline.
 *
 * Live (VITE_API_BASE set) → the app-server endpoints GET /api/models,
 * GET/PUT /api/models/assignments (api-endpoints.md). Otherwise → the in-repo
 * fixture (a session-scoped in-memory copy; no browser storage). The DTO shapes
 * are identical to the frontend types, so live JSON maps straight through.
 *
 * Module-size decomposition (TS-0003): the catalogue CRUD, device/agent-server
 * config, OpenRouter browse/key management, generic-provider management, and
 * misc platform toggles each moved to `./modelsParts/*` and are recomposed here
 * via import + thin delegation, so every existing import path (`@/api/models`)
 * and export signature is unchanged.
 */

export async function listModels(): Promise<ModelInfo[]> {
  return crud.listModels();
}

/** Add a model to the catalogue. */
export async function createModel(upsert: ModelUpsert): Promise<ModelInfo[]> {
  return crud.createModel(upsert);
}

/** Edit an existing catalogue model (id is immutable). */
export async function updateModel(id: string, upsert: ModelUpsert): Promise<ModelInfo[]> {
  return crud.updateModel(id, upsert);
}

/** Remove a model — refused if it's the default or assigned to a role. */
export async function deleteModel(id: string): Promise<ModelInfo[]> {
  return crud.deleteModel(id);
}

export async function getAssignments(): Promise<ModelAssignments> {
  return crud.getAssignments();
}

/**
 * Apply a partial assignment change. Assignments are ABSOLUTE — the system uses
 * exactly what is set; there is no validation/prediction here (capabilities are
 * advisory + fail-loud at runtime, not blocked in the UI).
 */
export async function updateAssignments(patch: AssignmentsPatch): Promise<ModelAssignments> {
  return crud.updateAssignments(patch);
}

export async function getSandboxConfig(): Promise<SandboxConfig> {
  return deviceConfig.getSandboxConfig();
}

export async function updateSandboxConfig(cfg: SandboxConfig): Promise<SandboxConfig> {
  return deviceConfig.updateSandboxConfig(cfg);
}

/** W-48 — connectivity preflight for a sandbox backend. Probed on the AGENT-server,
 * which owns the sandbox environment (the app-server has no container socket in a
 * split-container deploy, so its probe would falsely fail). Real, bounded probe →
 * a typed host-naming verdict (never throws for an expected failure). The fixture
 * path returns a synthetic "reachable". */
export async function testSandbox(cfg: SandboxConfig): Promise<ProbeResult> {
  return deviceConfig.testSandbox(cfg);
}

/** Reachability of the ACTIVE (persisted) sandbox backend — the cheap signal the app
 * shell polls to surface an unreachable sandbox BEFORE a doomed run. Probed on the
 * AGENT-server (which owns the sandbox environment), against the SAME service the run
 * path builds, so the banner and a real run agree. The fixture path returns a synthetic
 * "reachable" (dev = process backend). */
export async function getSandboxHealth(): Promise<SandboxHealth> {
  return deviceConfig.getSandboxHealth();
}

export async function getEncodersConfig(): Promise<EncodersConfig> {
  return deviceConfig.getEncodersConfig();
}

export async function updateEncodersConfig(cfg: EncodersConfig): Promise<EncodersConfig> {
  return deviceConfig.updateEncodersConfig(cfg);
}

export async function getTtsConfig(): Promise<TtsConfig> {
  return deviceConfig.getTtsConfig();
}

export async function updateTtsConfig(cfg: TtsConfig): Promise<TtsConfig> {
  return deviceConfig.updateTtsConfig(cfg);
}

export async function getImageGenConfig(): Promise<ImageGenConfig> {
  return deviceConfig.getImageGenConfig();
}

export async function updateImageGenConfig(cfg: ImageGenConfig): Promise<ImageGenConfig> {
  return deviceConfig.updateImageGenConfig(cfg);
}

export async function getDataSourcesConfig(): Promise<DataSourcesConfig> {
  return deviceConfig.getDataSourcesConfig();
}

export async function updateDataSourcesConfig(cfg: DataSourcesConfig): Promise<DataSourcesConfig> {
  return deviceConfig.updateDataSourcesConfig(cfg);
}

export async function getRoleFallbackConfig(): Promise<RoleFallbackConfig> {
  return deviceConfig.getRoleFallbackConfig();
}

export async function updateRoleFallbackConfig(
  cfg: RoleFallbackConfig,
): Promise<RoleFallbackConfig> {
  return deviceConfig.updateRoleFallbackConfig(cfg);
}

/** T4.2 — reachability of the configured search/extraction endpoint (app-server). */
export async function testDataSource(kind: "search" | "extraction"): Promise<ProbeResult> {
  return deviceConfig.testDataSource(kind);
}

/** T4.3 — synthesize a one-word clip via the configured TTS tier (agent-server). */
export async function testTts(): Promise<ProbeResult> {
  return deviceConfig.testTts();
}

/** T4.4 — one tiny generation via the configured image tier; reports NOT-CONFIGURED
 * honestly when no real backend is set up (W-50) (agent-server). */
export async function testImageGen(): Promise<ProbeResult> {
  return deviceConfig.testImageGen();
}

/** The live OpenRouter catalogue (hundreds of models), proxied by the app-server. */
export async function listOpenRouterModels(
  opts?: { allModalities?: boolean },
): Promise<OpenRouterModel[]> {
  return openRouter.listOpenRouterModels(opts);
}

export async function getOpenRouterKeyStatus(): Promise<OpenRouterKeyStatus> {
  return openRouter.getOpenRouterKeyStatus();
}

export async function setOpenRouterKey(key: string): Promise<OpenRouterKeyStatus> {
  return openRouter.setOpenRouterKey(key);
}

export async function clearOpenRouterKey(): Promise<OpenRouterKeyStatus> {
  return openRouter.clearOpenRouterKey();
}

/** Turn an OpenRouter model into a catalogue upsert (shared key + endpoint). */
export function openRouterUpsert(m: OpenRouterModel): ModelUpsert {
  return openRouter.openRouterUpsert(m);
}

export async function listProviderPresets(): Promise<ProviderPreset[]> {
  return providers.listProviderPresets();
}

export async function listProviders(): Promise<ProviderInfo[]> {
  return providers.listProviders();
}

export async function createProvider(body: ProviderCreate): Promise<ProviderMutationResult> {
  return providers.createProvider(body);
}

export async function updateProvider(
  id: string,
  patch: ProviderPatch,
): Promise<ProviderMutationResult> {
  return providers.updateProvider(id, patch);
}

export async function deleteProvider(id: string): Promise<void> {
  return providers.deleteProvider(id);
}

export async function listProviderModels(providerId: string): Promise<ProviderCatalogueModel[]> {
  return providers.listProviderModels(providerId);
}

export async function enableProviderModel(
  providerId: string,
  body: ProviderEnableBody,
): Promise<ModelInfo[]> {
  return providers.enableProviderModel(providerId, body);
}

export async function disableProviderModel(
  providerId: string,
  catalogueId: string,
): Promise<ModelInfo[]> {
  return providers.disableProviderModel(providerId, catalogueId);
}

export async function getLiveBrowserConfig(): Promise<LiveBrowserConfig> {
  return platformConfig.getLiveBrowserConfig();
}

export async function updateLiveBrowserConfig(cfg: LiveBrowserConfig): Promise<LiveBrowserConfig> {
  return platformConfig.updateLiveBrowserConfig(cfg);
}

export async function getBuildKernelConfig(): Promise<BuildKernelConfig> {
  return platformConfig.getBuildKernelConfig();
}

export async function updateBuildKernelConfig(
  cfg: Pick<BuildKernelConfig, "kind">,
): Promise<BuildKernelConfig> {
  return platformConfig.updateBuildKernelConfig(cfg);
}

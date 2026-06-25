import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  clearOpenRouterKey,
  createModel,
  deleteModel,
  getAssignments,
  getDataSourcesConfig,
  getEncodersConfig,
  getBuildKernelConfig,
  getImageGenConfig,
  getLiveBrowserConfig,
  getTtsConfig,
  getOpenRouterKeyStatus,
  getSandboxConfig,
  getSandboxHealth,
  listModels,
  listOpenRouterModels,
  setOpenRouterKey,
  updateAssignments,
  updateDataSourcesConfig,
  updateEncodersConfig,
  updateBuildKernelConfig,
  updateImageGenConfig,
  updateLiveBrowserConfig,
  updateTtsConfig,
  updateModel,
  updateSandboxConfig,
  testSandbox,
} from "@/api/models";
import type {
  AssignmentsPatch,
  BuildKernelConfig,
  DataSourcesConfig,
  EncodersConfig,
  ImageGenConfig,
  LiveBrowserConfig,
  TtsConfig,
  ModelAssignments,
  ModelInfo,
  ModelUpsert,
  OpenRouterKeyStatus,
  OpenRouterModel,
} from "@/types/models";
import type { SandboxConfig, SandboxHealth } from "@/types/sandbox";

/**
 * Query/mutation hooks for the model catalogue + assignments. Components consume
 * these — never the api layer directly (data-flow discipline). Query keys are
 * stable so the leader pill (Prompt 3) and the Settings matrix (Prompt 4) share
 * one cache.
 */

const MODELS_KEY = ["models"] as const;
const ASSIGNMENTS_KEY = ["model-assignments"] as const;

export function useModels() {
  return useQuery<ModelInfo[]>({ queryKey: MODELS_KEY, queryFn: listModels });
}

export function useAssignments() {
  return useQuery<ModelAssignments>({ queryKey: ASSIGNMENTS_KEY, queryFn: getAssignments });
}

export function useUpdateAssignments() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: AssignmentsPatch) => updateAssignments(patch),
    onSuccess: (next) => {
      // Write the authoritative result straight into the cache (no refetch flash).
      qc.setQueryData(ASSIGNMENTS_KEY, next);
    },
  });
}

const SANDBOX_KEY = ["sandbox-config"] as const;

export function useSandboxConfig() {
  return useQuery<SandboxConfig>({ queryKey: SANDBOX_KEY, queryFn: getSandboxConfig });
}

export function useUpdateSandboxConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (cfg: SandboxConfig) => updateSandboxConfig(cfg),
    onSuccess: (next) => qc.setQueryData(SANDBOX_KEY, next),
  });
}

/** W-48 — connectivity preflight for a sandbox backend. Returns the typed host-naming
 * ProbeResult; the Settings Save flow runs it so a misconfigured/unreachable backend
 * surfaces up-front instead of failing silently on the first build. */
export function useTestSandbox() {
  return useMutation({
    mutationFn: (cfg: SandboxConfig) => testSandbox(cfg),
  });
}

const SANDBOX_HEALTH_KEY = ["sandbox-health"] as const;

/** Reachability of the ACTIVE sandbox backend — polled by the app shell to surface an
 * unreachable sandbox BEFORE a doomed run. Refetches periodically + on window focus so a
 * host coming back (or going down) clears/raises the banner without a reload. */
export function useSandboxHealth() {
  return useQuery<SandboxHealth>({
    queryKey: SANDBOX_HEALTH_KEY,
    queryFn: getSandboxHealth,
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
    staleTime: 10_000,
  });
}

const ENCODERS_KEY = ["encoders-config"] as const;

export function useEncodersConfig() {
  return useQuery<EncodersConfig>({ queryKey: ENCODERS_KEY, queryFn: getEncodersConfig });
}

export function useUpdateEncodersConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (cfg: EncodersConfig) => updateEncodersConfig(cfg),
    onSuccess: (next) => qc.setQueryData(ENCODERS_KEY, next),
  });
}

const TTS_KEY = ["tts-config"] as const;

export function useTtsConfig() {
  return useQuery<TtsConfig>({ queryKey: TTS_KEY, queryFn: getTtsConfig });
}

export function useUpdateTtsConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (cfg: TtsConfig) => updateTtsConfig(cfg),
    onSuccess: (next) => qc.setQueryData(TTS_KEY, next),
  });
}

const IMAGE_GEN_KEY = ["image-gen-config"] as const;

export function useImageGenConfig() {
  return useQuery<ImageGenConfig>({ queryKey: IMAGE_GEN_KEY, queryFn: getImageGenConfig });
}

export function useUpdateImageGenConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (cfg: ImageGenConfig) => updateImageGenConfig(cfg),
    onSuccess: (next) => qc.setQueryData(IMAGE_GEN_KEY, next),
  });
}

const DATA_SOURCES_KEY = ["data-sources-config"] as const;

export function useDataSourcesConfig() {
  return useQuery<DataSourcesConfig>({
    queryKey: DATA_SOURCES_KEY,
    queryFn: getDataSourcesConfig,
  });
}

export function useUpdateDataSourcesConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (cfg: DataSourcesConfig) => updateDataSourcesConfig(cfg),
    onSuccess: (next) => qc.setQueryData(DATA_SOURCES_KEY, next),
  });
}

/** Catalogue CRUD. Each mutation returns the new catalogue, written straight into
 * the cache so the matrix + pickers update without a refetch flash. */
export function useCreateModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (upsert: ModelUpsert) => createModel(upsert),
    onSuccess: (models) => qc.setQueryData(MODELS_KEY, models),
  });
}

export function useUpdateModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, upsert }: { id: string; upsert: ModelUpsert }) => updateModel(id, upsert),
    onSuccess: (models) => qc.setQueryData(MODELS_KEY, models),
  });
}

export function useDeleteModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteModel(id),
    onSuccess: (models) => qc.setQueryData(MODELS_KEY, models),
  });
}

// ---- OpenRouter -----------------------------------------------------------

const OR_MODELS_KEY = ["openrouter-models"] as const;
const OR_KEY_KEY = ["openrouter-key"] as const;

/** The live OpenRouter catalogue. `enabled` gates the fetch to when the browse
 * dialog opens (don't pull hundreds of models on settings load). */
export function useOpenRouterModels(
  enabled: boolean,
  opts?: { allModalities?: boolean },
) {
  // A distinct cache key for the `all` variant so the image-gen picker (which needs
  // image-output generators) never collides with the LLM browser's text-only fetch.
  const all = !!opts?.allModalities;
  return useQuery<OpenRouterModel[]>({
    queryKey: all ? [...OR_MODELS_KEY, "all"] : OR_MODELS_KEY,
    queryFn: () => listOpenRouterModels({ allModalities: all }),
    enabled,
    staleTime: 10 * 60 * 1000, // the catalogue changes slowly; cache 10 min
  });
}

export function useOpenRouterKey() {
  return useQuery<OpenRouterKeyStatus>({ queryKey: OR_KEY_KEY, queryFn: getOpenRouterKeyStatus });
}

export function useSetOpenRouterKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => setOpenRouterKey(key),
    onSuccess: (status) => qc.setQueryData(OR_KEY_KEY, status),
  });
}

export function useClearOpenRouterKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => clearOpenRouterKey(),
    onSuccess: (status) => qc.setQueryData(OR_KEY_KEY, status),
  });
}

/** Convenience: look a model up by id from a fetched catalogue. */
export function findModel(models: ModelInfo[] | undefined, id: string | null): ModelInfo | null {
  if (!models || !id) return null;
  return models.find((m) => m.id === id) ?? null;
}

// ---- live browser (noVNC) toggle ------------------------------------------

const LIVE_BROWSER_KEY = ["live-browser-config"] as const;

export function useLiveBrowserConfig() {
  return useQuery<LiveBrowserConfig>({
    queryKey: LIVE_BROWSER_KEY,
    queryFn: getLiveBrowserConfig,
  });
}

export function useUpdateLiveBrowserConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (cfg: LiveBrowserConfig) => updateLiveBrowserConfig(cfg),
    onSuccess: (next) => qc.setQueryData(LIVE_BROWSER_KEY, next),
  });
}

const BUILD_KERNEL_KEY = ["build-kernel-config"] as const;

export function useBuildKernelConfig() {
  return useQuery<BuildKernelConfig>({
    queryKey: BUILD_KERNEL_KEY,
    queryFn: getBuildKernelConfig,
  });
}

export function useUpdateBuildKernelConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (cfg: Pick<BuildKernelConfig, "kind">) => updateBuildKernelConfig(cfg),
    onSuccess: (next) => qc.setQueryData(BUILD_KERNEL_KEY, next),
  });
}

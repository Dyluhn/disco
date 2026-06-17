/**
 * Provider-secrets hooks — list / set / clear encrypted-at-rest API keys by name.
 * Mirrors the OpenRouter-key hooks (useModels.ts) for the generic surface.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { clearSecret, listSecrets, setSecret, type SecretsList } from "@/api/secrets";
import { useDataSourcesConfig, useModels, useTtsConfig } from "@/hooks/useModels";

const SECRETS_KEY = ["secrets"] as const;

// The OpenRouter key has its own dedicated section/route; don't list it as a
// generic provider key the user must add here.
const RESERVED_ENV = new Set(["DISCO_OPENROUTER_API_KEY", "PMX_OPENROUTER_API_KEY"]);

/** The env-var NAMES referenced by the user's CURRENT provider config — the
 * paid models' `api_key_env`, plus the search / extraction / TTS key envs.
 * These are the keys the running app will look for; cross-referencing them with
 * the stored secrets tells the user which ones are still missing. */
export function useExpectedKeyNames(): string[] {
  const { data: models } = useModels();
  const { data: ds } = useDataSourcesConfig();
  const { data: tts } = useTtsConfig();
  const names = new Set<string>();
  for (const m of models ?? []) if (m.api_key_env) names.add(m.api_key_env);
  if (ds?.search_api_key_env) names.add(ds.search_api_key_env);
  if (ds?.extraction_api_key_env) names.add(ds.extraction_api_key_env);
  if (tts?.api_key_env) names.add(tts.api_key_env);
  return [...names].filter((n) => n && !RESERVED_ENV.has(n)).sort();
}

export function useSecrets() {
  return useQuery<SecretsList>({ queryKey: SECRETS_KEY, queryFn: listSecrets });
}

export function useSetSecret() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, value }: { name: string; value: string }) => setSecret(name, value),
    onSuccess: () => qc.invalidateQueries({ queryKey: SECRETS_KEY }),
  });
}

export function useClearSecret() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => clearSecret(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: SECRETS_KEY }),
  });
}

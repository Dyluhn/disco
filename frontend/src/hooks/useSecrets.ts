/**
 * Provider-secrets hooks — list / set / clear encrypted-at-rest API keys by name.
 * Mirrors the OpenRouter-key hooks (useModels.ts) for the generic surface.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { clearSecret, listSecrets, setSecret, type SecretsList } from "@/api/secrets";

const SECRETS_KEY = ["secrets"] as const;

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

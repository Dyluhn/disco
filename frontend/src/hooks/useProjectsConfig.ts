/**
 * Project Storage settings hook — fetches the configured projects_root + live
 * validation status. Mirrors `useSandboxConfig` / `useUpdateSandboxConfig`
 * exactly.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getProjectsConfig, updateProjectsConfig } from "@/api/projects";
import type { ProjectStorageConfig } from "@/types/project";

const PROJECTS_CONFIG_KEY = ["projects-config"] as const;

export function useProjectsConfig() {
  return useQuery<ProjectStorageConfig>({
    queryKey: PROJECTS_CONFIG_KEY,
    queryFn: getProjectsConfig,
  });
}

export function useUpdateProjectsConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (cfg: ProjectStorageConfig) => updateProjectsConfig(cfg),
    onSuccess: (next) => qc.setQueryData(PROJECTS_CONFIG_KEY, next),
  });
}

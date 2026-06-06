/**
 * Build-project list + delete + download hooks. Mirrors `useConversations` /
 * `useDeleteConversation` so the Projects view stays a thin component on top
 * of TanStack Query, no direct fetch.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { deleteProject, downloadProject, listProjects } from "@/api/projects";
import type { ProjectsList } from "@/types/project";

export const PROJECTS_KEY = ["projects"] as const;

export function useProjects() {
  return useQuery<ProjectsList>({ queryKey: PROJECTS_KEY, queryFn: listProjects });
}

export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteProject(id),
    onSuccess: ({ id }) => {
      qc.setQueryData<ProjectsList>(PROJECTS_KEY, (prev) =>
        prev ? { ...prev, projects: prev.projects.filter((p) => p.id !== id) } : prev,
      );
    },
  });
}

export function useDownloadProject() {
  return useMutation({
    mutationFn: (id: string) => downloadProject(id),
  });
}

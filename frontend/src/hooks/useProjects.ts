/**
 * Build-project list + delete + download hooks. Mirrors `useConversations` /
 * `useDeleteConversation` so the Projects view stays a thin component on top
 * of TanStack Query, no direct fetch.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deleteProject,
  downloadProject,
  exportProjectManifest,
  getProjectManifest,
  getProjectRelease,
  listProjects,
  type DownloadBinding,
} from "@/api/projects";
import type { ProjectManifest, ProjectsList } from "@/types/project";
import type { ReleaseResponse } from "@/types/release";

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

/** Download a project's workspace zip. The mutation variable carries the optional
 * SOURCE binding: a self-host `candidate` passes its concrete `{version_seq,
 * spec_digest}` so the download is pinned to that immutable version; a plain
 * (unbound) download passes `binding: null`. `title` is presentation only — it
 * names the SAVED FILE (UI-40) and never reaches the request. */
export function useDownloadProject() {
  return useMutation({
    mutationFn: (vars: { id: string; binding: DownloadBinding | null; title?: string | null }) =>
      downloadProject(vars.id, vars.binding, vars.title),
  });
}

export function useExportManifest() {
  return useMutation({
    mutationFn: (id: string) => exportProjectManifest(id),
  });
}

/** The committed-workspace manifest for a project.
 *
 * UI-7: a manifest only EXISTS once the run has committed a workspace. Asking
 * for one mid-run answered 404 on every visit and logged a console error, so
 * callers that know the run has not committed yet pass `available: false` and
 * nothing is requested. A late 404 is still possible (the commit and this
 * request can race), so the query does not retry it into three errors either. */
export function useProjectManifest(conversationId: string | null, available = true) {
  return useQuery<ProjectManifest>({
    queryKey: ["project-manifest", conversationId],
    queryFn: () => getProjectManifest(conversationId!),
    enabled: !!conversationId && available,
    retry: false,
  });
}

/** The WO-7 release verdict for a project (can it be self-hosted, what env NAMES
 * it needs, its ingress). Gated on a non-null id like `useProjectManifest`; offline
 * it resolves the in-repo fixture verdict so the capabilities UI works with no
 * backend. */
export function useProjectRelease(conversationId: string | null) {
  return useQuery<ReleaseResponse>({
    queryKey: ["project-release", conversationId],
    queryFn: () => getProjectRelease(conversationId!),
    enabled: !!conversationId,
  });
}

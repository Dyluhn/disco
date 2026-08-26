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
import { releaseSealKey, type CommittedFinish } from "@/lib/committedFinish";

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
 * (unbound) download passes `binding: null`. */
export function useDownloadProject() {
  return useMutation({
    mutationFn: (vars: { id: string; binding: DownloadBinding | null }) =>
      downloadProject(vars.id, vars.binding),
  });
}

export function useExportManifest() {
  return useMutation({
    mutationFn: (id: string) => exportProjectManifest(id),
  });
}

export function useProjectManifest(conversationId: string | null) {
  return useQuery<ProjectManifest>({
    queryKey: ["project-manifest", conversationId],
    queryFn: () => getProjectManifest(conversationId!),
    enabled: !!conversationId,
  });
}

/** The WO-7 release verdict for a project (can it be self-hosted, what env NAMES
 * it needs, its ingress). Gated on a non-null id like `useProjectManifest`; offline
 * it resolves the in-repo fixture verdict so the capabilities UI works with no
 * backend. */
export class ReleaseSealMismatchError extends Error {
  readonly name = "ReleaseSealMismatchError";

  constructor(
    readonly expected: Pick<CommittedFinish, "versionSeq" | "treeDigest">,
    readonly actual: Pick<ReleaseResponse, "version_seq" | "tree_digest">,
  ) {
    super("release response does not match the committed workspace seal");
  }
}

/**
 * Fetch the release verdict. The one-argument form is retained for the Projects
 * list, which is a historical manifest-backed surface and has no event replay.
 * Build/Agent passes the second argument explicitly (including null while the
 * seal is still pending), which makes the query strictly seal-gated.
 */
export function useProjectRelease(
  conversationId: string | null,
  committedFinish?: CommittedFinish | null,
) {
  const sealAware = committedFinish !== undefined;
  return useQuery<ReleaseResponse>({
    queryKey: sealAware
      ? releaseSealKey(conversationId, committedFinish ?? null)
      : ["project-release", conversationId],
    queryFn: async () => {
      const response = await getProjectRelease(conversationId!);
      if (sealAware && committedFinish) {
        if (
          response.version_seq !== committedFinish.versionSeq ||
          response.tree_digest !== committedFinish.treeDigest
        ) {
          throw new ReleaseSealMismatchError(committedFinish, response);
        }
      }
      return response;
    },
    enabled: !!conversationId && (!sealAware || !!committedFinish),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

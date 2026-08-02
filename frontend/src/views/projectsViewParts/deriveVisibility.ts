/**
 * Pure derivations extracted from ProjectsView's conditional-rendering block.
 * Each function's branching is exactly what it was in the component — only
 * relocated, so it's measured on its own instead of piling onto ProjectsView's
 * cyclomatic count.
 */
import type { ProjectStorageStatus } from "@/types/project";

export function deriveShowConfigureStorage(
  isLoading: boolean,
  isError: boolean,
  status: ProjectStorageStatus,
): boolean {
  return !isLoading && !isError && status !== "ok";
}

/** Shared by the search bar and the projects list — both render only once
 * storage is configured, the query settled, and there's at least one project. */
export function deriveShowProjectsContent(
  isLoading: boolean,
  isError: boolean,
  status: ProjectStorageStatus,
  allLength: number,
): boolean {
  return !isLoading && !isError && status === "ok" && allLength > 0;
}

export function deriveShowEmptyProjects(
  isLoading: boolean,
  isError: boolean,
  status: ProjectStorageStatus,
  allLength: number,
): boolean {
  return !isLoading && !isError && status === "ok" && allLength === 0;
}

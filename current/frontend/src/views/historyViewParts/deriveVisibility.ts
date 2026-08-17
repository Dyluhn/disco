/**
 * Pure derivations extracted from HistoryView's conditional-rendering block.
 * Each function's branching is exactly what it was in the component — only
 * relocated, so it's measured on its own instead of piling onto HistoryView's
 * cyclomatic count.
 */

/** The `useConversations` query param for the active space filter. */
export function deriveQuerySpaceId(
  spaceFilter: "all" | "unfiled" | string,
): string | null | undefined {
  return spaceFilter === "all" ? undefined : spaceFilter === "unfiled" ? null : spaceFilter;
}

export function deriveShowFilterBar(
  isLoading: boolean,
  isError: boolean,
  allLength: number,
  spaceFilter: "all" | "unfiled" | string,
): boolean {
  return !isLoading && !isError && (allLength > 0 || spaceFilter !== "all");
}

export function deriveShowEmptyHistory(
  isLoading: boolean,
  isError: boolean,
  allLength: number,
  spaceFilter: "all" | "unfiled" | string,
): boolean {
  return !isLoading && !isError && allLength === 0 && spaceFilter === "all";
}

export function deriveShowEmptyForSpace(
  isLoading: boolean,
  isError: boolean,
  allLength: number,
  spaceFilter: "all" | "unfiled" | string,
): boolean {
  return !isLoading && !isError && allLength === 0 && spaceFilter !== "all";
}

export function deriveShowList(isLoading: boolean, isError: boolean, allLength: number): boolean {
  return !isLoading && !isError && allLength > 0;
}

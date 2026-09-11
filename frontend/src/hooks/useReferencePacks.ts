import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  addReferencePackFiles,
  deleteReferencePack,
  getReferencePack,
  listReferencePacks,
  removeReferencePackFile,
  updateReferencePack,
} from "@/api/referencePacks";
import type { ReferencePackDetail, ReferencePackSummary } from "@/types/referencePacks";

export const REFERENCE_PACKS_KEY = ["reference-packs"] as const;

export function useReferencePacks() {
  return useQuery<ReferencePackSummary[]>({
    queryKey: REFERENCE_PACKS_KEY,
    queryFn: listReferencePacks,
  });
}

export function useReferencePack(packId: string | null) {
  return useQuery<ReferencePackDetail>({
    queryKey: [...REFERENCE_PACKS_KEY, packId],
    queryFn: () => getReferencePack(packId as string),
    enabled: packId !== null,
  });
}

function useInvalidating<TArgs extends unknown[], TResult>(
  fn: (...args: TArgs) => Promise<TResult>,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: TArgs) => fn(...args),
    onSuccess: () => qc.invalidateQueries({ queryKey: REFERENCE_PACKS_KEY }),
  });
}

export function useUpdateReferencePack() {
  return useInvalidating(updateReferencePack);
}

export function useAddReferencePackFiles() {
  return useInvalidating(addReferencePackFiles);
}

export function useRemoveReferencePackFile() {
  return useInvalidating(removeReferencePackFile);
}

export function useDeleteReferencePack() {
  return useInvalidating(deleteReferencePack);
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  bindReferencePacks,
  createReferencePack,
  deleteReferencePack,
  getReferencePackBinding,
  listReferencePacks,
  updateReferencePack,
  updateReferencePackFiles,
  type ReferencePackDraft,
} from "@/api/referencePacks";

export const REFERENCE_PACKS_KEY = ["reference-packs"] as const;

export function useReferencePacks() {
  return useQuery({ queryKey: REFERENCE_PACKS_KEY, queryFn: listReferencePacks });
}

export function useCreateReferencePack() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (draft: ReferencePackDraft) => createReferencePack(draft),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: REFERENCE_PACKS_KEY }),
  });
}

export function useUpdateReferencePack() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Partial<ReferencePackDraft> }) =>
      updateReferencePack(id, patch),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: REFERENCE_PACKS_KEY }),
  });
}

export function useDeleteReferencePack() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteReferencePack(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: REFERENCE_PACKS_KEY }),
  });
}

export function useUpdateReferencePackFiles() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, files, remove }: { id: string; files: File[]; remove?: string[] }) =>
      updateReferencePackFiles(id, files, remove),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: REFERENCE_PACKS_KEY }),
  });
}

export function useBindReferencePacks() {
  return useMutation({
    mutationFn: ({
      conversationId,
      selections,
    }: {
      conversationId: string;
      selections: Array<{ pack_id: string; version_id: string; content_sha256: string }>;
    }) => bindReferencePacks(conversationId, selections),
  });
}

export function useReferencePackBinding(conversationId: string | null) {
  return useQuery({
    queryKey: ["reference-pack-binding", conversationId],
    queryFn: () => getReferencePackBinding(conversationId ?? ""),
    enabled: Boolean(conversationId),
  });
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createSpace,
  deleteSpace,
  getSpace,
  listSpaces,
  uploadSpaceDocuments,
} from "@/api/spaces";
import type { CreateSpaceInput, SpaceUploadResult } from "@/types/spaces";

export function useSpaces() {
  return useQuery({ queryKey: ["spaces"], queryFn: listSpaces });
}

export function useSpace(spaceId: string | null) {
  return useQuery({
    queryKey: ["space", spaceId],
    queryFn: () => getSpace(spaceId!),
    enabled: !!spaceId,
  });
}

export function useCreateSpace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateSpaceInput) => createSpace(input),
    onSuccess: async (space) => {
      await qc.invalidateQueries({ queryKey: ["spaces"] });
      qc.setQueryData(["space", space.space_id], space);
    },
  });
}

export function useDeleteSpace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteSpace,
    onSuccess: async ({ space_id }) => {
      await qc.invalidateQueries({ queryKey: ["spaces"] });
      qc.removeQueries({ queryKey: ["space", space_id] });
    },
  });
}

export function useUploadSpaceDocuments(spaceId: string | null) {
  const qc = useQueryClient();
  return useMutation<SpaceUploadResult, Error, File[]>({
    mutationFn: (files) => uploadSpaceDocuments(spaceId!, files),
    onSuccess: async (result) => {
      qc.setQueryData(["space", result.space.space_id], result.space);
      await qc.invalidateQueries({ queryKey: ["spaces"] });
    },
  });
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createSpace,
  deleteSpace,
  getSpace,
  listSpaces,
  renameSpace,
} from "@/api/spaces";
import type { CreateSpaceInput, RenameSpaceInput } from "@/types/spaces";

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

export function useRenameSpace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: RenameSpaceInput) => renameSpace(input),
    onSuccess: async (space) => {
      qc.setQueryData(["space", space.space_id], space);
      await qc.invalidateQueries({ queryKey: ["spaces"] });
    },
  });
}

export function useDeleteSpace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteSpace,
    onSuccess: async ({ space_id }) => {
      await qc.invalidateQueries({ queryKey: ["spaces"] });
      await qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.removeQueries({ queryKey: ["space", space_id] });
    },
  });
}

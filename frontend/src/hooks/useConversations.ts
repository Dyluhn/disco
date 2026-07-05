import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deleteConversation,
  listConversations,
  setConversationSpace,
} from "@/api/conversations";
import type { ConversationSummary } from "@/types/conversation";

/** Query/mutation hooks for the owner-scoped conversation library (data-flow
 *  discipline — components never touch the api layer directly). */

const CONVERSATIONS_KEY = ["conversations"] as const;

function conversationsKey(spaceId?: string | null) {
  return spaceId === undefined
    ? CONVERSATIONS_KEY
    : [...CONVERSATIONS_KEY, spaceId ?? "unfiled"] as const;
}

export function useConversations(spaceId?: string | null) {
  return useQuery<ConversationSummary[]>({
    queryKey: conversationsKey(spaceId),
    queryFn: () => listConversations(spaceId),
  });
}

export function useDeleteConversation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: ({ id }) => {
      qc.setQueriesData<ConversationSummary[]>(
        { queryKey: CONVERSATIONS_KEY },
        (prev) => (prev ?? []).filter((c) => c.id !== id),
      );
    },
  });
}

export function useSetConversationSpace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, spaceId }: { id: string; spaceId: string | null }) =>
      setConversationSpace(id, spaceId),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: CONVERSATIONS_KEY });
      await qc.invalidateQueries({ queryKey: ["spaces"] });
      await qc.invalidateQueries({ queryKey: ["space"] });
    },
  });
}

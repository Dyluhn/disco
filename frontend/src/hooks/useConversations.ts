import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { deleteConversation, listConversations } from "@/api/conversations";
import type { ConversationSummary } from "@/types/conversation";

/** Query/mutation hooks for the owner-scoped conversation library (data-flow
 *  discipline — components never touch the api layer directly). */

const CONVERSATIONS_KEY = ["conversations"] as const;

export function useConversations() {
  return useQuery<ConversationSummary[]>({
    queryKey: CONVERSATIONS_KEY,
    queryFn: listConversations,
  });
}

export function useDeleteConversation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: ({ id }) => {
      qc.setQueryData<ConversationSummary[]>(CONVERSATIONS_KEY, (prev) =>
        (prev ?? []).filter((c) => c.id !== id),
      );
    },
  });
}

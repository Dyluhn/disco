import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { listMcpConnections, listSkills, setSkillEnabled } from "@/api/config";
import type { McpConnection, Skill } from "@/types/config";

/** Query/mutation hooks for the skills + MCP scaffolds (data-flow discipline). */

const SKILLS_KEY = ["skills"] as const;
const MCP_KEY = ["mcp-connections"] as const;

export function useSkills() {
  return useQuery<Skill[]>({ queryKey: SKILLS_KEY, queryFn: listSkills });
}

export function useToggleSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      setSkillEnabled(id, enabled),
    onSuccess: (next) => qc.setQueryData(SKILLS_KEY, next),
  });
}

export function useMcpConnections() {
  return useQuery<McpConnection[]>({ queryKey: MCP_KEY, queryFn: listMcpConnections });
}

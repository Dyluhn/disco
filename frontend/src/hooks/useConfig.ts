import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createSkill,
  deleteSkill,
  listMcpConnections,
  listSkills,
  updateSkill,
} from "@/api/config";
import type { McpConnection, Skill, SkillCreate, SkillPatch } from "@/types/config";

/** Query/mutation hooks for the skills subsystem + the MCP scaffold. The skills
 *  mutations invalidate the list so the UI reflects the persisted state. */

const SKILLS_KEY = ["skills"] as const;
const MCP_KEY = ["mcp-connections"] as const;

export function useSkills() {
  return useQuery<Skill[]>({ queryKey: SKILLS_KEY, queryFn: listSkills });
}

export function useCreateSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (create: SkillCreate) => createSkill(create),
    onSuccess: () => qc.invalidateQueries({ queryKey: SKILLS_KEY }),
  });
}

export function useUpdateSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: SkillPatch }) => updateSkill(id, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: SKILLS_KEY }),
  });
}

export function useDeleteSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteSkill(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: SKILLS_KEY }),
  });
}

/** Back-compat alias: the old toggle hook, now backed by updateSkill. */
export function useToggleSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      updateSkill(id, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: SKILLS_KEY }),
  });
}

export function useMcpConnections() {
  return useQuery<McpConnection[]>({ queryKey: MCP_KEY, queryFn: listMcpConnections });
}

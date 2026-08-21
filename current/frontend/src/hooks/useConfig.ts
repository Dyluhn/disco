import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  approveMcpServer,
  createMcpServer as apiCreateMcp,
  createSkill,
  deleteMcpServer as apiDeleteMcp,
  deleteSkill,
  importMcpConfig,
  listMcpConnections,
  listSkills,
  revokeMcpServer as apiRevokeMcp,
  updateMcpServer as apiUpdateMcp,
  updateSkill,
} from "@/api/config";
import type { McpConnection, McpServerApprove, McpServerConfig, Skill, SkillCreate, SkillPatch } from "@/types/config";

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

export function useCreateMcpServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (config: McpServerConfig) => apiCreateMcp(config),
    onSuccess: () => qc.invalidateQueries({ queryKey: MCP_KEY }),
  });
}

/** Paste-a-config import. Applies (dryRun=false) invalidate the connection
 * list; previews are read-only and leave the cache alone. */
export function useImportMcpConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ text, dryRun }: { text: string; dryRun: boolean }) =>
      importMcpConfig(text, dryRun),
    onSuccess: (_result, variables) => {
      if (!variables.dryRun) void qc.invalidateQueries({ queryKey: MCP_KEY });
    },
  });
}

export function useUpdateMcpServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, patch }: { name: string; patch: McpServerConfig }) =>
      apiUpdateMcp(name, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: MCP_KEY }),
  });
}

export function useDeleteMcpServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => apiDeleteMcp(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: MCP_KEY }),
  });
}

export function useApproveMcpServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: McpServerApprove }) =>
      approveMcpServer(name, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: MCP_KEY }),
  });
}

export function useRevokeMcpServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => apiRevokeMcp(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: MCP_KEY }),
  });
}

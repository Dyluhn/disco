import { useState } from "react";
import type { WorkflowMcpMountInput } from "@/types/workflow";
import { toggleValue } from "./draftMapping";

export function useAuthorMcpMounts() {
  const [mcpMounts, setMcpMounts] = useState<WorkflowMcpMountInput[]>([]);

  function addMcpMount(defaultServer: string | undefined) {
    if (!defaultServer) return;
    setMcpMounts((current) => [
      ...current,
      { server: defaultServer, tool_names: [], read_only: true },
    ]);
  }

  function updateMountServer(index: number, server: string) {
    setMcpMounts((current) =>
      current.map((item, itemIndex) =>
        itemIndex === index ? { ...item, server, tool_names: [] } : item,
      ),
    );
  }

  function updateMountReadOnly(index: number, readOnly: boolean) {
    setMcpMounts((current) =>
      current.map((item, itemIndex) =>
        itemIndex === index ? { ...item, read_only: readOnly } : item,
      ),
    );
  }

  function toggleMountTool(index: number, tool: string, checked: boolean) {
    setMcpMounts((current) =>
      current.map((item, itemIndex) =>
        itemIndex === index
          ? { ...item, tool_names: toggleValue(item.tool_names, tool, checked) }
          : item,
      ),
    );
  }

  function removeMount(index: number) {
    setMcpMounts((current) => current.filter((_, i) => i !== index));
  }

  const emptyMcpMount = mcpMounts.some((mount) => mount.tool_names.length === 0);
  const hasWritableMount = mcpMounts.some((mount) => !mount.read_only);

  return {
    mcpMounts,
    setMcpMounts,
    addMcpMount,
    updateMountServer,
    updateMountReadOnly,
    toggleMountTool,
    removeMount,
    emptyMcpMount,
    hasWritableMount,
  };
}

export type AuthorMcpMountsState = ReturnType<typeof useAuthorMcpMounts>;

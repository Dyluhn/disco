import { useState, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { restoreWorkspaceVersion } from "@/api/agent";
import { restoreFailureCopy } from "@/api/preview";
import { useToast } from "@/components/toastApi";

export interface WorkspaceRollbackState {
  restoreNotice: { tone: "success" | "error"; text: string } | null;
  restoringSeq: number | null;
  selectVersion: (seq: number | null) => void;
  rollBackToVersion: (seq: number) => Promise<void>;
}

/** Owns the read-only-version selection + rollback flow: `restoreNotice`,
 * `restoringSeq`, and the `selectVersion` / `rollBackToVersion` actions that
 * drive them. Takes the shared route ref + refresh-nonce/selected-version
 * setters from the coordinator since both this flow and the launch mint need
 * to agree on "what are we looking at right now". */
export function useWorkspaceRollback(
  cid: string | null,
  refetchVersions: () => Promise<unknown>,
  setSelectedVersionSeq: Dispatch<SetStateAction<number | null>>,
  currentRouteRef: MutableRefObject<string>,
  setRefreshNonce: Dispatch<SetStateAction<number>>,
): WorkspaceRollbackState {
  const toast = useToast();
  const [restoringSeq, setRestoringSeq] = useState<number | null>(null);
  const [restoreNotice, setRestoreNotice] = useState<{
    tone: "success" | "error";
    text: string;
  } | null>(null);

  function selectVersion(seq: number | null) {
    setSelectedVersionSeq(seq);
    setRestoreNotice(null);
    currentRouteRef.current = "/";
    setRefreshNonce((value) => value + 1);
  }

  async function rollBackToVersion(seq: number) {
    if (!cid || restoringSeq !== null) return;
    setRestoringSeq(seq);
    setRestoreNotice(null);
    try {
      const result = await restoreWorkspaceVersion(cid, seq);
      await refetchVersions();
      setSelectedVersionSeq(null);
      currentRouteRef.current = "/";
      setRefreshNonce((value) => value + 1);
      const text =
        result.new_version == null
          ? `Rolled back to v${seq}.`
          : `Rolled back to v${seq}; saved as v${result.new_version}.`;
      setRestoreNotice({ tone: "success", text });
      toast.show({
        title: `Rolled back to v${seq}`,
        body: result.new_version == null ? undefined : `Saved as v${result.new_version}.`,
      });
    } catch (error) {
      setRestoreNotice({ tone: "error", text: restoreFailureCopy(error) });
    } finally {
      setRestoringSeq(null);
    }
  }

  return { restoreNotice, restoringSeq, selectVersion, rollBackToVersion };
}

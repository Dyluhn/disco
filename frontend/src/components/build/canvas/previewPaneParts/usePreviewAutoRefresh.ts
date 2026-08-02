import { useEffect, useMemo, useRef, type Dispatch, type SetStateAction } from "react";
import type { WorkspaceFile } from "@/lib/buildTrace";
import type { ConversationStatus } from "@/types/agent";

/** Plain/static runtimes need a frame refresh when the workspace changes. HMR
 * runtimes own their connection and remain mounted so Vite/Next can update
 * naturally without losing component or route state. */
export function usePreviewAutoRefresh(
  files: WorkspaceFile[],
  status: ConversationStatus,
  reloadStrategy: "hmr" | "reload" | undefined,
  setRefreshNonce: Dispatch<SetStateAction<number>>,
): void {
  const fileSignature = useMemo(
    () => JSON.stringify(files.map((file) => [file.path, file.bytes, file.content])),
    [files],
  );
  const lastFileSignature = useRef(fileSignature);
  useEffect(() => {
    if (status !== "RUNNING") {
      lastFileSignature.current = fileSignature;
      return;
    }
    if (fileSignature === lastFileSignature.current) return;
    if (reloadStrategy === "hmr") {
      lastFileSignature.current = fileSignature;
      return;
    }
    const timer = window.setTimeout(() => {
      lastFileSignature.current = fileSignature;
      setRefreshNonce((value) => value + 1);
    }, 600);
    return () => window.clearTimeout(timer);
  }, [reloadStrategy, fileSignature, status, setRefreshNonce]);
}

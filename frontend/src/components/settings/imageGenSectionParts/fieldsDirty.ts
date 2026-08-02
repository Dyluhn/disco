import type { ImageGenConfig } from "@/types/models";

/** Whether the draft fields differ from the last-saved config — gates the Save
 * button and the live-probe's "unsaved changes" disabled hint. */
export function computeImageGenFieldsDirty(
  data: ImageGenConfig | undefined,
  baseUrl: string,
  apiKeyEnv: string,
  model: string,
  workflowJson: string,
): boolean {
  return (
    !!data &&
    (baseUrl !== (data.base_url ?? "") ||
      apiKeyEnv !== (data.api_key_env ?? "") ||
      model !== (data.model ?? "") ||
      workflowJson !== (data.workflow_json ?? ""))
  );
}

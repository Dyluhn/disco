/**
 * Pure derived-state helpers for EncoderSection — pulled out of the component
 * purely to shed cyclomatic complexity (TS-0029). No hooks, no transport.
 */

import type { EncodersConfig } from "@/types/models";

export interface EncoderUrls {
  embedder_url: string;
  reranker_url: string;
  nli_url: string;
}

/** True when the local URL draft has diverged from the persisted config — the
 * same three-field comparison the component used to inline. */
export function isEncoderUrlsDirty(
  urls: EncoderUrls,
  data: EncodersConfig | undefined,
): boolean {
  return (
    !!data &&
    (urls.embedder_url !== (data.embedder_url ?? "") ||
      urls.reranker_url !== (data.reranker_url ?? "") ||
      urls.nli_url !== (data.nli_url ?? ""))
  );
}

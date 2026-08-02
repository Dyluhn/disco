import type { TtsConfig } from "@/types/models";

/** Whether the draft fields differ from the last-saved config — gates the Save
 * button and the live-probe's "unsaved changes" disabled hint. */
export function computeAudioFieldsDirty(
  data: TtsConfig | undefined,
  baseUrl: string,
  apiKeyEnv: string,
  model: string,
  voiceA: string,
  voiceB: string,
): boolean {
  return (
    !!data &&
    (baseUrl !== (data.base_url ?? "") ||
      apiKeyEnv !== (data.api_key_env ?? "") ||
      model !== (data.model ?? "") ||
      voiceA !== (data.voice_a ?? "") ||
      voiceB !== (data.voice_b ?? ""))
  );
}

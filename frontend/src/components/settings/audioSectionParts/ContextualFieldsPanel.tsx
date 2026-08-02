import { testTts } from "@/api/models";
import { ProbeButton } from "../ProbeButton";
import { EndpointUrlField } from "./EndpointUrlField";
import { PaidFieldsRow } from "./PaidFieldsRow";
import { SaveRow } from "./SaveRow";
import { VoiceFieldsRow } from "./VoiceFieldsRow";

/** Contextual fields — voices (any on-mode) + endpoint (self-host/paid) + key+model
 * (paid) + the live probe, shown below the mode picker whenever audio is enabled. */
export function ContextualFieldsPanel({
  enabled,
  showUrl,
  showPaid,
  baseUrl,
  setBaseUrl,
  apiKeyEnv,
  setApiKeyEnv,
  model,
  setModel,
  voiceA,
  setVoiceA,
  voiceB,
  setVoiceB,
  fieldsDirty,
  savePending,
  onSave,
  agentIsLive,
}: {
  enabled: boolean;
  showUrl: boolean;
  showPaid: boolean;
  baseUrl: string;
  setBaseUrl: (value: string) => void;
  apiKeyEnv: string;
  setApiKeyEnv: (value: string) => void;
  model: string;
  setModel: (value: string) => void;
  voiceA: string;
  setVoiceA: (value: string) => void;
  voiceB: string;
  setVoiceB: (value: string) => void;
  fieldsDirty: boolean;
  savePending: boolean;
  onSave: () => void;
  agentIsLive: boolean;
}) {
  if (!enabled) return null;

  const probeDisabledHint = fieldsDirty
    ? "save changes to test"
    : "connect the agent server to test";

  return (
    <div className="mt-hair flex flex-col gap-inline rounded-card border border-hairline bg-surface-1/40 px-body py-inline">
      {showUrl && (
        <EndpointUrlField
          showPaid={showPaid}
          baseUrl={baseUrl}
          setBaseUrl={setBaseUrl}
        />
      )}
      {showPaid && (
        <PaidFieldsRow
          apiKeyEnv={apiKeyEnv}
          setApiKeyEnv={setApiKeyEnv}
          model={model}
          setModel={setModel}
        />
      )}
      <VoiceFieldsRow
        voiceA={voiceA}
        setVoiceA={setVoiceA}
        voiceB={voiceB}
        setVoiceB={setVoiceB}
      />
      <SaveRow fieldsDirty={fieldsDirty} savePending={savePending} onSave={onSave} />
      {/* T4.3 live probe: synthesize the single word "Disco" via the
          SAVED TTS tier. Non-empty audio = ok; a disabled/unreachable/
          bad-key tier fails honestly. Disabled until field edits are saved
          and the agent-server is connected. */}
      <ProbeButton
        control="settings.audio-test-tts"
        idleLabel="Test TTS"
        run={testTts}
        disabled={!agentIsLive || fieldsDirty}
        disabledHint={probeDisabledHint}
      />
    </div>
  );
}

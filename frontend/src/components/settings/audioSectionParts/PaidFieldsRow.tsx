import { FIELD_CLASS } from "./styles";

export function PaidFieldsRow({
  apiKeyEnv,
  setApiKeyEnv,
  model,
  setModel,
}: {
  apiKeyEnv: string;
  setApiKeyEnv: (value: string) => void;
  model: string;
  setModel: (value: string) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-inline">
      <label className="flex flex-col gap-hair">
        <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
          Stored key name
          <span className="font-ui text-[0.72rem] text-text-faint">
            · advanced
          </span>
        </span>
        <input
          spellCheck={false}
          value={apiKeyEnv}
          onChange={(e) => setApiKeyEnv(e.target.value)}
          placeholder="OPENAI_API_KEY"
          className={FIELD_CLASS}
        />
      </label>
      <label className="flex flex-col gap-hair">
        <span className="font-ui text-[0.8rem] text-text">Model</span>
        <input
          spellCheck={false}
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="tts-1"
          className={FIELD_CLASS}
        />
      </label>
    </div>
  );
}

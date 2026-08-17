import { FIELD_CLASS } from "./styles";
import { StoredCredentialField } from "../StoredCredentialField";

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
      <StoredCredentialField
        label="Audio API credential"
        value={apiKeyEnv}
        onChange={setApiKeyEnv}
        placeholder="OPENAI_API_KEY"
        optional={false}
        inputClassName={FIELD_CLASS}
      />
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

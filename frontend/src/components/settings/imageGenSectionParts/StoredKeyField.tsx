import { FIELD_CLASS } from "./styles";

export function StoredKeyField({
  apiKeyEnv,
  setApiKeyEnv,
}: {
  apiKeyEnv: string;
  setApiKeyEnv: (value: string) => void;
}) {
  return (
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
  );
}

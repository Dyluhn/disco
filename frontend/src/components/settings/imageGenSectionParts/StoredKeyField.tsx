import { StoredCredentialField } from "../StoredCredentialField";
import { FIELD_CLASS } from "./styles";

export function StoredKeyField({
  apiKeyEnv,
  setApiKeyEnv,
}: {
  apiKeyEnv: string;
  setApiKeyEnv: (value: string) => void;
}) {
  return (
    <StoredCredentialField
      label="Image API credential"
      value={apiKeyEnv}
      onChange={setApiKeyEnv}
      placeholder="OPENAI_API_KEY"
      optional={false}
      inputClassName={FIELD_CLASS}
    />
  );
}

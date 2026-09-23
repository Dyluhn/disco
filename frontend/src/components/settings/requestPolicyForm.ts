import { useState } from "react";
import type { ModelUpsert, RequestPolicy } from "@/types/models";

export function useRequestPolicyForm(initial: RequestPolicy | undefined) {
  const [text, updateText] = useState(JSON.stringify(initial ?? {}, null, 2));
  const [error, setError] = useState<string | null>(null);
  const parse = (): RequestPolicy | null => {
    try {
      const value: unknown = JSON.parse(text);
      if (!value || Array.isArray(value) || typeof value !== "object") throw new Error();
      setError(null);
      return value as RequestPolicy;
    } catch {
      setError("Request options must be a JSON object.");
      return null;
    }
  };
  const setText = (value: string) => { updateText(value); setError(null); };
  return { text, setText, error, parse };
}

export function modelUpsertPayload(form: ModelUpsert, policy: RequestPolicy): ModelUpsert {
  return {
    ...form,
    request_policy: policy,
    base_url: form.base_url?.trim() || null,
    api_key_env: form.api_key_env?.trim() || null,
    max_output_tokens: form.max_output_tokens || null,
    quantization: form.quantization?.trim() || null,
  };
}

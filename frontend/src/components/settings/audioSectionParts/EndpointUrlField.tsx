import { cn } from "@/lib/cn";
import { TAP_TARGET } from "@/lib/tapTarget";
import { FIELD_CLASS } from "./styles";

export function EndpointUrlField({
  showPaid,
  baseUrl,
  setBaseUrl,
}: {
  showPaid: boolean;
  baseUrl: string;
  setBaseUrl: (value: string) => void;
}) {
  return (
    <label className="flex flex-col gap-hair">
      <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
        Endpoint base URL
        <span className="font-ui text-[0.72rem] text-text-faint">
          · OpenAI-shape /v1/audio/speech
        </span>
      </span>
      <input
        type="url"
        inputMode="url"
        spellCheck={false}
        value={baseUrl}
        onChange={(e) => setBaseUrl(e.target.value)}
        placeholder={
          showPaid
            ? "https://api.openai.com  (empty = OpenAI default)"
            : "http://host:port  (empty = server default)"
        }
        className={cn(FIELD_CLASS, TAP_TARGET)}
      />
    </label>
  );
}

import type { useRequestPolicyForm } from "./requestPolicyForm";

export function RequestPolicyEditor({ editor }: { editor: ReturnType<typeof useRequestPolicyForm> }) {
  return <div className="flex flex-col gap-hair">
    <label htmlFor="model-request-options" className="font-ui text-[0.74rem] font-medium uppercase tracking-wide text-text-faint">
      Request options (JSON)
    </label>
    <textarea id="model-request-options" rows={8} value={editor.text}
      className="w-full rounded-control border border-hairline bg-surface-1 px-inline py-hair font-mono text-sm text-text"
      onChange={(e) => editor.setText(e.target.value)} spellCheck={false} />
    <p className="text-sm text-text-faint">
      Optional body, reasoning_enabled, reasoning_disabled, headers, and session_header
      use the fields your endpoint accepts. Empty settings leave its defaults unchanged.
      Names and URLs never select controls. headers carries non-secret static headers
      (e.g. User-Agent); session_header names one extra header receiving the stable
      session identity. Do not put credentials here.
    </p>
    {editor.error && <p role="alert">{editor.error}</p>}
  </div>;
}

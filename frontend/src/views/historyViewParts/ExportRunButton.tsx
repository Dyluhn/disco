import { Download, Loader2 } from "lucide-react";
import { useState } from "react";
import { exportConversationBundle } from "@/api/agentParts/share";

export function ExportRunButton({ cid, title }: { cid: string; title: string }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const download = async () => {
    setPending(true);
    setError(null);
    try {
      const bundle = await exportConversationBundle(cid);
      const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `${cid}-run.json`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setError("Couldn't export this run. Try again after reconnecting.");
    } finally { setPending(false); }
  };
  return <div className="flex max-w-40 flex-col gap-hair">
    <button type="button" aria-label={`Export run: ${title}`} title="Download a redacted run bundle for read-only import"
      data-disco-control="history.export-run" disabled={pending} onClick={() => void download()}
      className="grid size-11 shrink-0 place-items-center rounded-control border border-hairline text-text-faint hover:text-text disabled:opacity-50 lg:size-8">
      {pending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Download className="size-4" aria-hidden />}
    </button>
    {error && <p role="alert" className="font-ui text-xs text-warn">{error}</p>}
  </div>;
}

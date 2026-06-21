/**
 * BP-11 upload composer: a paperclip button + drag-and-drop overlay for the
 * Build surface's input area. Keeps the upload logic isolated and testable.
 */

import { useRef, useState } from "react";
import { Paperclip } from "lucide-react";
import { cn } from "@/lib/cn";
import { uploadFiles } from "@/api/agent";
import { useToast } from "@/components/Toast";

interface UploadComposerProps {
  cid: string | null;
  /** Called after a successful upload so the parent can react (e.g. re-kick). */
  onUploaded?: () => void;
}

export function UploadComposer({ cid, onUploaded }: UploadComposerProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const { show } = useToast();
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);

  async function handleFiles(files: FileList | File[] | null) {
    if (!files || !cid) return;
    const list = Array.from(files);
    if (list.length === 0) return;
    setBusy(true);
    try {
      const result = await uploadFiles(cid, list);
      if (result.saved.length > 0) {
        show({ title: `Uploaded ${result.saved.length} file(s) to uploads/` });
        onUploaded?.();
      }
      for (const r of result.rejected) {
        show({ title: `Rejected: ${r.name}`, body: r.reason, tone: "neutral" });
      }
    } catch (err) {
      show({
        title: "Upload failed",
        body: err instanceof Error ? err.message : String(err),
        tone: "neutral",
      });
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  function onDragOver(e: React.DragEvent) {
    e.preventDefault();
    setDragging(true);
  }

  function onDragLeave(e: React.DragEvent) {
    // Only clear dragging when leaving the element itself (not a child)
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
      setDragging(false);
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    void handleFiles(e.dataTransfer.files);
  }

  return (
    <div
      role="group"
      aria-label="Upload files"
      data-testid="upload-composer"
      data-dragging={dragging || undefined}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      className={cn(
        "relative rounded-control transition-colors",
        dragging && "outline outline-2 outline-accent/60 bg-accent/5",
      )}
    >
      {dragging && (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-control text-[0.82rem] font-ui text-accent"
        >
          Drop files to upload
        </div>
      )}
      <button
        type="button"
        disabled={busy || !cid}
        onClick={() => inputRef.current?.click()}
        aria-label="Attach files"
        title="Upload files to uploads/"
        data-disco-control="upload-files"
        className={cn(
          "flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text disabled:opacity-40",
          busy && "opacity-50",
        )}
      >
        <Paperclip className="size-3.5" aria-hidden />
        {busy ? "Uploading…" : "Attach"}
      </button>
      <input
        ref={inputRef}
        type="file"
        multiple
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        onChange={(e) => void handleFiles(e.target.files)}
      />
    </div>
  );
}

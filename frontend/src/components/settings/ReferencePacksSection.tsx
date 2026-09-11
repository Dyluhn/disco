/**
 * Settings → Reference packs: the user's reusable file collections. Lists what
 * exists (name, description, file count, size, updated), and edits it — rename,
 * describe, inspect files with a truthful readability state, add/replace/remove
 * files, delete with a confirmation. Creation lives in the Agent (attach files,
 * ask for a pack); there is no second creation flow here.
 */
import { useRef, useState } from "react";
import { BookMarked, ChevronDown, Pencil, Trash2, Upload } from "lucide-react";
import { cn } from "@/lib/cn";
import { TAP_TARGET_ICON } from "@/lib/tapTarget";
import { referencePackFilePath } from "@/api/referencePacks";
import {
  useAddReferencePackFiles,
  useDeleteReferencePack,
  useReferencePack,
  useReferencePacks,
  useRemoveReferencePackFile,
  useUpdateReferencePack,
} from "@/hooks/useReferencePacks";
import type { ReferencePackFileState, ReferencePackSummary } from "@/types/referencePacks";

const STATE_LABEL: Record<ReferencePackFileState, string> = {
  ready: "Ready",
  asset_only: "Asset only",
  unreadable: "Couldn't read",
};

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

const inputClass =
  "min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.88rem] text-text outline-none focus:border-accent/60";
const buttonClass =
  "flex min-h-11 items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text disabled:opacity-50 lg:min-h-0";

function PackRow({ pack }: { pack: ReferencePackSummary }) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(pack.name);
  const [description, setDescription] = useState(pack.description);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const detail = useReferencePack(open ? pack.pack_id : null);
  const update = useUpdateReferencePack();
  const addFiles = useAddReferencePackFiles();
  const removeFile = useRemoveReferencePackFile();
  const remove = useDeleteReferencePack();
  const busy = update.isPending || addFiles.isPending || removeFile.isPending || remove.isPending;

  const run = (p: Promise<unknown>) => {
    setError(null);
    p.catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  };

  return (
    <div className="flex flex-col gap-hair rounded-card border border-hairline bg-surface-1 p-body">
      <div className="flex items-start gap-inline">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-start gap-hair text-left"
        >
          <ChevronDown
            className={cn("mt-1 size-3.5 shrink-0 text-text-faint transition-transform", open && "rotate-180")}
            aria-hidden
          />
          <span className="flex min-w-0 flex-col">
            <span className="truncate font-ui text-[0.92rem] font-medium text-text">{pack.name}</span>
            {pack.description && (
              <span className="font-ui text-[0.78rem] text-text-muted">{pack.description}</span>
            )}
            <span className="font-ui text-[0.72rem] text-text-faint">
              {pack.file_count} file{pack.file_count === 1 ? "" : "s"} · {formatBytes(pack.total_bytes)}{" "}
              · updated {new Date(pack.updated_at).toLocaleString()}
            </span>
          </span>
        </button>
        <button
          type="button"
          aria-label={`Edit ${pack.name}`}
          onClick={() => {
            setEditing((e) => !e);
            setOpen(true);
          }}
          className={cn(TAP_TARGET_ICON, "text-text-muted hover:text-text")}
        >
          <Pencil className="size-3.5" aria-hidden />
        </button>
        <button
          type="button"
          aria-label={`Delete ${pack.name}`}
          onClick={() => setConfirmDelete(true)}
          className={cn(TAP_TARGET_ICON, "text-text-muted hover:text-weak")}
        >
          <Trash2 className="size-3.5" aria-hidden />
        </button>
      </div>

      {confirmDelete && (
        <div className="flex flex-col gap-hair rounded-control border border-weak/40 bg-surface-2 p-inline font-ui text-[0.8rem]">
          <span>
            Delete “{pack.name}”? Future Builds can no longer select it. Builds that already
            pinned it keep their copy and stay resumable.
          </span>
          <div className="flex gap-hair">
            <button
              type="button"
              disabled={busy}
              onClick={() => run(remove.mutateAsync([pack.pack_id]))}
              className={cn(buttonClass, "border-weak/50 text-weak")}
            >
              Delete pack
            </button>
            <button type="button" onClick={() => setConfirmDelete(false)} className={buttonClass}>
              Keep it
            </button>
          </div>
        </div>
      )}

      {editing && (
        <div className="flex flex-col gap-hair">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            aria-label="Pack name"
            className={inputClass}
          />
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            aria-label="Pack description"
            placeholder="What these files are for"
            className={inputClass}
          />
          <div className="flex gap-hair">
            <button
              type="button"
              disabled={busy || name.trim().length === 0}
              onClick={() =>
                run(
                  update
                    .mutateAsync([pack.pack_id, { name: name.trim(), description }])
                    .then(() => setEditing(false)),
                )
              }
              className={cn(buttonClass, "border-accent/50 text-accent")}
            >
              Save
            </button>
            <button type="button" onClick={() => setEditing(false)} className={buttonClass}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {open && (
        <div className="flex flex-col gap-hair">
          {detail.data?.files.map((file) => (
            <div
              key={file.name}
              className="flex items-center gap-inline rounded-control bg-surface-2 px-inline py-hair font-ui text-[0.8rem]"
            >
              <a
                href={referencePackFilePath(pack.pack_id, file.name)}
                target="_blank"
                rel="noreferrer"
                className="min-w-0 flex-1 truncate text-text hover:underline"
              >
                {file.name}
              </a>
              <span className="text-text-faint">{formatBytes(file.bytes)}</span>
              <span
                className={cn(
                  "rounded-control border px-hair text-[0.7rem]",
                  file.state === "ready" && "border-supported/40 text-supported",
                  file.state === "asset_only" && "border-hairline text-text-muted",
                  file.state === "unreadable" && "border-weak/40 text-weak",
                )}
                title={
                  file.state === "ready"
                    ? "The agent can read this as text"
                    : file.state === "asset_only"
                      ? "Pixels or a PDF without extractable text — the agent gets the file, not its text"
                      : "Neither text nor a known asset type"
                }
              >
                {STATE_LABEL[file.state]}
              </span>
              <button
                type="button"
                aria-label={`Remove ${file.name}`}
                disabled={busy || (detail.data?.files.length ?? 0) <= 1}
                onClick={() => run(removeFile.mutateAsync([pack.pack_id, file.name]))}
                className={cn(TAP_TARGET_ICON, "text-text-muted hover:text-weak disabled:opacity-40")}
              >
                <Trash2 className="size-3.5" aria-hidden />
              </button>
            </div>
          ))}
          <div className="flex items-center gap-hair">
            <input
              ref={fileInput}
              type="file"
              multiple
              className="hidden"
              aria-label={`Add files to ${pack.name}`}
              onChange={(e) => {
                const files = Array.from(e.target.files ?? []);
                e.target.value = "";
                if (files.length) run(addFiles.mutateAsync([pack.pack_id, files]));
              }}
            />
            <button
              type="button"
              disabled={busy}
              onClick={() => fileInput.current?.click()}
              className={buttonClass}
            >
              <Upload className="size-3.5" aria-hidden />
              Add or replace files
            </button>
            <span className="font-ui text-[0.72rem] text-text-faint">
              Same name replaces. 25 MB per file, 50 files, 200 MB per pack.
            </span>
          </div>
        </div>
      )}
      {error && <div className="font-ui text-[0.78rem] text-weak">{error}</div>}
    </div>
  );
}

export function ReferencePacksSection() {
  const { data: packs = [], isLoading, error } = useReferencePacks();
  return (
    <div className="flex flex-col gap-inline" data-testid="reference-packs-section">
      <div className="flex items-center gap-hair">
        <BookMarked className="size-4 text-text-faint" aria-hidden />
        <h3 className="font-ui text-[0.95rem] font-medium text-text">Reference packs</h3>
      </div>
      <p className="font-ui text-[0.8rem] text-text-muted">
        Reusable collections of your files — a brand kit, a spec, sample data — that a Build can
        select. To make one: in an Agent task, attach the files and ask the Agent to create a
        reference pack from them. A Build copies the exact files it selected, so editing a pack
        here changes future Builds only.
      </p>
      {isLoading && <div className="font-ui text-[0.8rem] text-text-muted">Loading…</div>}
      {error && (
        <div className="font-ui text-[0.8rem] text-weak">
          Couldn’t load reference packs: {error instanceof Error ? error.message : String(error)}
        </div>
      )}
      {!isLoading && !error && packs.length === 0 && (
        <div className="rounded-card border border-dashed border-hairline p-body font-ui text-[0.8rem] text-text-muted">
          No reference packs yet.
        </div>
      )}
      {packs.map((pack) => (
        <PackRow key={pack.pack_id} pack={pack} />
      ))}
    </div>
  );
}

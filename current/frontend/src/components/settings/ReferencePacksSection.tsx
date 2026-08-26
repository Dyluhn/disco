import { Pencil, Trash2 } from "lucide-react";
import { useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import {
  useDeleteReferencePack,
  useReferencePacks,
  useUpdateReferencePackFiles,
  useUpdateReferencePack,
} from "@/hooks/useReferencePacks";
import type { ReferencePack } from "@/api/referencePacks";

function PackEditor({
  pack,
  busy,
  onSave,
  onCancel,
}: {
  pack: ReferencePack;
  busy: boolean;
  onSave: (name: string, description: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(pack.name);
  const [description, setDescription] = useState(pack.description);
  const valid = name.trim().length > 0;
  return (
    <form
      className="flex flex-col gap-inline"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid) onSave(name.trim(), description.trim());
      }}
    >
      <input
        value={name}
        onChange={(event) => setName(event.target.value)}
        aria-label="Reference Pack name"
        className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.88rem] text-text outline-none focus:border-accent lg:min-h-0"
      />
      <input
        value={description}
        onChange={(event) => setDescription(event.target.value)}
        aria-label="Reference Pack description"
        placeholder="Short description"
        className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent lg:min-h-0"
      />
      <div className="flex items-center justify-end gap-inline">
        <button type="button" onClick={onCancel} className="min-h-11 rounded-control px-inline py-hair font-ui text-[0.8rem] text-text-muted hover:text-text lg:min-h-0">Cancel</button>
        <button type="submit" disabled={!valid || busy} className="min-h-11 rounded-control bg-accent px-body py-hair font-ui text-[0.8rem] font-medium text-bg transition-opacity disabled:opacity-40 lg:min-h-0">
          {busy ? "Saving…" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

function PackFilesEditor({
  pack,
  busy,
  onSave,
}: {
  pack: ReferencePack;
  busy: boolean;
  onSave: (files: File[], remove: string[], onSuccess: () => void) => void;
}) {
  const files = pack.files ?? pack.current?.files ?? [];
  const [pending, setPending] = useState<File[]>([]);
  const [removed, setRemoved] = useState<string[]>([]);
  const pathFor = (file: File) =>
    (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
  const visibleFiles = files.filter((file) => !removed.includes(file.path ?? file.name));
  const changed = pending.length > 0 || removed.length > 0;
  return (
    <details className="mt-inline rounded-control border border-hairline/70 px-inline py-hair">
      <summary className="cursor-pointer font-ui text-[0.76rem] text-text-muted">Files ({files.length})</summary>
      <div className="mt-hair flex flex-col gap-hair border-t border-hairline/60 pt-hair">
        <ul className="flex flex-col gap-hair">
        {visibleFiles.map((file) => {
          const path = file.path ?? file.name;
          return (
          <li key={`${path}:${file.sha256}`} className="flex items-center justify-between gap-inline font-mono text-[0.72rem] text-text-faint">
            <span className="min-w-0 truncate" title={file.path ?? file.name}>{file.path ?? file.name}</span>
            <span className="flex shrink-0 items-center gap-hair">
              <span>{file.media_type}</span>
              <button
                type="button"
                aria-label={`Remove ${path}`}
                onClick={() => setRemoved((current) => [...current, path])}
                className="rounded-control px-hair text-text-muted hover:text-unsupported"
              >×</button>
            </span>
          </li>
          );
        })}
        {pending.map((file) => (
          <li key={`pending:${pathFor(file)}`} className="flex items-center justify-between gap-inline font-ui text-[0.72rem] text-accent">
            <span className="min-w-0 truncate" title={pathFor(file)}>Queued: {pathFor(file)}</span>
            <span className="shrink-0">{file.size.toLocaleString()} bytes</span>
          </li>
        ))}
        </ul>
        <div className="flex flex-wrap items-center gap-inline pt-hair">
          <label className="min-h-9 cursor-pointer rounded-control border border-hairline px-inline py-hair font-ui text-[0.76rem] text-text-muted hover:border-accent hover:text-text">
            Add or replace files
            <input
              type="file"
              multiple
              aria-label={`Add or replace files for ${pack.name}`}
              className="sr-only"
              onChange={(event) => {
                const next = Array.from(event.target.files ?? []);
                setPending((current) => {
                  const merged = new Map(current.map((file) => [pathFor(file), file]));
                  for (const file of next) merged.set(pathFor(file), file);
                  return [...merged.values()];
                });
                setRemoved((current) => current.filter((path) => !next.some((file) => pathFor(file) === path)));
                event.target.value = "";
              }}
            />
          </label>
          {changed && (
            <button
              type="button"
              disabled={busy}
              onClick={() => onSave(pending, removed, () => { setPending([]); setRemoved([]); })}
              className="min-h-9 rounded-control bg-accent px-inline py-hair font-ui text-[0.76rem] font-medium text-bg disabled:opacity-40"
            >
              {busy ? "Saving files…" : "Save files"}
            </button>
          )}
        </div>
      </div>
    </details>
  );
}

export function ReferencePacksSection() {
  const { data: packs, isLoading } = useReferencePacks();
  const update = useUpdateReferencePack();
  const updateFiles = useUpdateReferencePackFiles();
  const remove = useDeleteReferencePack();
  const [editingId, setEditingId] = useState<string | null>(null);
  const error = update.error ?? updateFiles.error ?? remove.error;

  return (
    <section aria-labelledby="reference-packs-heading" className="flex flex-col gap-inline">
      <div className="flex items-center justify-between gap-inline">
        <h3 id="reference-packs-heading" className="font-ui text-[0.95rem] font-semibold text-text">Reference Packs</h3>
      </div>
      <p className="font-ui text-[0.84rem] text-text-muted">
        Reusable reference libraries for Build and Agent. Create a pack from an
        Agent session by asking it to add reference files. This page manages the
        saved pack name, description, files, and deletion.
      </p>
      <p className="font-ui text-[0.76rem] text-text-faint">
        A Build receives an immutable snapshot when you select a pack. Editing or
        deleting the library later does not change an existing Build.
      </p>
      {error && <p role="alert" className="font-ui text-[0.8rem] text-unsupported">Reference Pack change failed: {error instanceof Error ? error.message : String(error)}</p>}
      {isLoading && <p className="rounded-card border border-hairline bg-surface-1 px-body py-body font-ui text-[0.84rem] text-text-muted">Loading Reference Packs…</p>}
      {!isLoading && (packs ?? []).length === 0 && <p className="rounded-card border border-dashed border-hairline bg-surface-1 px-body py-body text-center font-ui text-[0.84rem] text-text-faint">No Reference Packs yet. Ask the Agent to create one from files in its workspace.</p>}
      <ul className="flex flex-col gap-inline">
        {(packs ?? []).map((pack) => (
          <li key={pack.id} className="rounded-card border border-hairline bg-surface-1 px-body py-body">
            {editingId === pack.id ? (
              <PackEditor
                pack={pack}
                busy={update.isPending}
                onCancel={() => setEditingId(null)}
                onSave={(name, description) => update.mutate({ id: pack.id, patch: { name, description } }, { onSuccess: () => setEditingId(null) })}
              />
            ) : (
              <div className="flex items-start justify-between gap-body">
                <div className="min-w-0 flex-1">
                  <p className="font-ui text-[0.88rem] font-medium text-text">{pack.name}</p>
                  <p className="mt-hair font-ui text-[0.78rem] text-text-muted">{pack.description || "No description"}</p>
                  <p className="mt-hair font-ui text-[0.72rem] text-text-faint">
                    {pack.files.length} {pack.files.length === 1 ? "file" : "files"} · {pack.files.reduce((total, file) => total + file.size, 0).toLocaleString()} bytes · updated {new Date(pack.updated_at).toLocaleString()}
                  </p>
                  <PackFilesEditor
                    pack={pack}
                    busy={updateFiles.isPending}
                    onSave={(files, removed, onSuccess) =>
                      updateFiles.mutate({ id: pack.id, files, remove: removed }, { onSuccess })
                    }
                  />
                </div>
                <div className="flex shrink-0 items-center gap-hair">
                  <button type="button" aria-label={`Edit ${pack.name}`} onClick={() => setEditingId(pack.id)} className="rounded-control border border-hairline p-hair text-text-muted hover:border-accent hover:text-text"><Pencil className="size-3.5" aria-hidden /></button>
                  <ConfirmDialog
                    trigger={<button type="button" aria-label={`Delete ${pack.name}`} className="rounded-control border border-hairline p-hair text-text-muted hover:border-unsupported hover:text-unsupported"><Trash2 className="size-3.5" aria-hidden /></button>}
                    title={`Delete ${pack.name}?`}
                    description="This removes the library entry. Existing Build conversations keep their immutable snapshots."
                    confirmLabel="Delete Pack"
                    confirmContext="reference-pack-delete"
                    onConfirm={() => remove.mutate(pack.id)}
                  />
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

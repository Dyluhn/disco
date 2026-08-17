/**
 * Create/Rename Space dialogs extracted from SpacesView (module-size row:
 * SpacesView was 503 logical lines against a 500 cap). Relocated verbatim —
 * behavior, DOM, and copy are unchanged.
 */
import * as Dialog from "@radix-ui/react-dialog";
import { Loader2, Pencil, Plus, X } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { useCreateSpace, useRenameSpace } from "@/hooks/useSpaces";
import type { SpaceDetail } from "@/types/spaces";

export function CreateSpaceDialog() {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const create = useCreateSpace();

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const clean = name.trim();
    if (!clean) return;
    create.mutate(
      { name: clean, description },
      {
        onSuccess: () => {
          setName("");
          setDescription("");
          setOpen(false);
        },
      },
    );
  };

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button
          type="button"
          data-disco-control="spaces.create"
          className="flex items-center gap-hair rounded-control bg-accent px-body py-hair font-ui text-[0.84rem] text-bg transition-opacity hover:opacity-90"
        >
          <Plus className="size-4" aria-hidden />
          New Space
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex w-[min(32rem,92vw)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-card border border-hairline bg-bg p-body pmx-rise">
          <div className="flex items-start justify-between gap-inline">
            <div>
              <Dialog.Title className="font-ui text-[0.98rem] font-semibold text-text">
                Create Space
              </Dialog.Title>
              <Dialog.Description className="mt-hair font-ui text-[0.8rem] text-text-muted">
                Create a folder for related conversations and runs.
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <button
                type="button"
                aria-label="Close"
                className="grid size-7 place-items-center rounded-control text-text-faint transition-colors hover:bg-surface-2 hover:text-text"
              >
                <X className="size-4" aria-hidden />
              </button>
            </Dialog.Close>
          </div>
          <form onSubmit={submit} className="mt-body flex flex-col gap-inline">
            <label className="flex flex-col gap-hair font-ui text-[0.78rem] text-text-muted">
              Name
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
                className="rounded-control border border-hairline bg-surface-1 px-inline py-hair text-[0.9rem] text-text outline-none focus:border-hairline-strong"
              />
            </label>
            <label className="flex flex-col gap-hair font-ui text-[0.78rem] text-text-muted">
              Description
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
                className="resize-none rounded-control border border-hairline bg-surface-1 px-inline py-hair text-[0.9rem] text-text outline-none focus:border-hairline-strong"
              />
            </label>
            {create.error && (
              <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
                {create.error instanceof Error ? create.error.message : "Could not create Space."}
              </p>
            )}
            <div className="flex justify-end gap-inline pt-inline">
              <Dialog.Close asChild>
                <button
                  type="button"
                  className="rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:text-text"
                >
                  Cancel
                </button>
              </Dialog.Close>
              <button
                type="submit"
                disabled={!name.trim() || create.isPending}
                className="flex items-center gap-hair rounded-control bg-accent px-body py-hair font-ui text-[0.84rem] text-bg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {create.isPending && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
                Create
              </button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function RenameSpaceDialog({ space }: { space: SpaceDetail }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(space.name);
  const [description, setDescription] = useState(space.description);
  const rename = useRenameSpace();

  useEffect(() => {
    if (!open) {
      setName(space.name);
      setDescription(space.description);
    }
  }, [open, space.name, space.description]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const clean = name.trim();
    if (!clean) return;
    rename.mutate(
      { spaceId: space.space_id, name: clean, description },
      { onSuccess: () => setOpen(false) },
    );
  };

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button
          type="button"
          aria-label={`Rename Space: ${space.name}`}
          data-disco-control="spaces.rename"
          className="grid size-8 place-items-center rounded-control border border-hairline text-text-faint transition-colors hover:text-text"
        >
          <Pencil className="size-4" aria-hidden />
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex w-[min(32rem,92vw)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-card border border-hairline bg-bg p-body pmx-rise">
          <div className="flex items-start justify-between gap-inline">
            <Dialog.Title className="font-ui text-[0.98rem] font-semibold text-text">
              Rename Space
            </Dialog.Title>
            <Dialog.Close asChild>
              <button
                type="button"
                aria-label="Close"
                className="grid size-7 place-items-center rounded-control text-text-faint transition-colors hover:bg-surface-2 hover:text-text"
              >
                <X className="size-4" aria-hidden />
              </button>
            </Dialog.Close>
          </div>
          <form onSubmit={submit} className="mt-body flex flex-col gap-inline">
            <label className="flex flex-col gap-hair font-ui text-[0.78rem] text-text-muted">
              Name
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
                className="rounded-control border border-hairline bg-surface-1 px-inline py-hair text-[0.9rem] text-text outline-none focus:border-hairline-strong"
              />
            </label>
            <label className="flex flex-col gap-hair font-ui text-[0.78rem] text-text-muted">
              Description
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
                className="resize-none rounded-control border border-hairline bg-surface-1 px-inline py-hair text-[0.9rem] text-text outline-none focus:border-hairline-strong"
              />
            </label>
            {rename.error && (
              <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
                {rename.error instanceof Error ? rename.error.message : "Could not rename Space."}
              </p>
            )}
            <div className="flex justify-end gap-inline pt-inline">
              <Dialog.Close asChild>
                <button
                  type="button"
                  className="rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:text-text"
                >
                  Cancel
                </button>
              </Dialog.Close>
              <button
                type="submit"
                disabled={!name.trim() || rename.isPending}
                className="flex items-center gap-hair rounded-control bg-accent px-body py-hair font-ui text-[0.84rem] text-bg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {rename.isPending && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
                Save
              </button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * Server-side directory picker — navigate the APP HOST's filesystem (not the
 * browser machine's) to pick a project storage directory. Uses /api/storage/
 * browse, which only returns directory listings (never file contents).
 *
 * UI: breadcrumb + parent button + entry list (dirs are clickable to descend,
 * files are visually muted). Bottom action: "Choose this directory" applies the
 * CURRENT path, with a live validity check shown inline so a non-writable pick
 * is named before saving.
 */

import * as Dialog from "@radix-ui/react-dialog";
import { ArrowUp, CheckCircle2, Folder, FolderPlus, FileText, XCircle } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useMutation } from "@tanstack/react-query";
import { browseStorage } from "@/api/projects";
import { cn } from "@/lib/cn";
import type { BrowseResult, ProjectStorageStatus } from "@/types/project";

const STATUS_LABEL: Record<ProjectStorageStatus, { ok: boolean; text: string }> = {
  ok: { ok: true, text: "Selectable" },
  unset: { ok: false, text: "Empty path" },
  not_found: { ok: false, text: "Not found" },
  not_a_directory: { ok: false, text: "Not a directory" },
  not_writable: { ok: false, text: "Not writable" },
};

export function PathPickerDialog({
  trigger,
  initialPath,
  onChoose,
}: {
  trigger: ReactNode;
  initialPath: string;
  onChoose: (path: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState<string>(initialPath || "");
  const browse = useMutation<BrowseResult, Error, string>({
    mutationFn: (p: string) => browseStorage(p),
    onSuccess: (data) => setCurrent(data.path),
  });

  // load when the dialog opens (so reopening picks up changes), and whenever
  // current changes (the descend/parent buttons drive this via mutate).
  useEffect(() => {
    if (!open) return;
    browse.mutate(initialPath || "");
    // intentionally omit `browse`/`initialPath` from deps; the open transition is the trigger
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const data = browse.data;
  const status = data?.selectable ?? "unset";
  const statusInfo = STATUS_LABEL[status];

  const confirm = () => {
    if (!data || data.selectable !== "ok") return;
    onChoose(data.path);
    setOpen(false);
  };

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex h-[min(34rem,86vh)] w-[min(36rem,92vw)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-card border border-hairline bg-bg pmx-rise">
          <div className="border-b border-hairline px-body py-inline">
            <Dialog.Title className="flex items-center gap-hair font-ui text-[0.95rem] font-semibold text-text">
              <FolderPlus className="size-4 text-accent" aria-hidden />
              Choose project storage directory
            </Dialog.Title>
            <Dialog.Description className="mt-hair font-ui text-[0.78rem] text-text-faint">
              This navigates the host where the app is running. Project files persist on that
              machine.
            </Dialog.Description>
          </div>

          {/* path breadcrumb + parent */}
          <div className="flex items-center gap-inline border-b border-hairline px-body py-hair">
            <button
              type="button"
              onClick={() => data?.parent && browse.mutate(data.parent)}
              disabled={!data?.parent || browse.isPending}
              aria-label="Parent directory"
              className="grid size-7 place-items-center rounded-control border border-hairline text-text-muted transition-colors hover:text-text disabled:opacity-40"
            >
              <ArrowUp className="size-3.5" aria-hidden />
            </button>
            <code className="min-w-0 flex-1 truncate font-mono text-[0.8rem] text-text">
              {data?.path ?? current ?? "—"}
            </code>
          </div>

          {/* listing */}
          <div className="min-h-0 flex-1 overflow-y-auto">
            {browse.isPending && (
              <div className="px-body py-body font-ui text-[0.82rem] text-text-faint">
                Loading…
              </div>
            )}
            {browse.isError && (
              <div className="px-body py-body font-ui text-[0.82rem] text-unsupported">
                {browse.error.message || "Failed to read this directory."}
              </div>
            )}
            {data && (
              <ul className="divide-y divide-hairline">
                {data.entries.length === 0 && (
                  <li className="px-body py-body font-ui text-[0.82rem] text-text-faint">
                    (empty directory)
                  </li>
                )}
                {data.entries.map((entry) => (
                  <li key={entry.name}>
                    <button
                      type="button"
                      onClick={() =>
                        entry.is_dir &&
                        browse.mutate(`${data.path.replace(/\/$/, "")}/${entry.name}`)
                      }
                      disabled={!entry.is_dir}
                      className={cn(
                        "flex w-full items-center gap-inline px-body py-hair text-left font-ui text-[0.84rem] transition-colors",
                        entry.is_dir
                          ? "text-text hover:bg-surface-1"
                          : "cursor-default text-text-faint",
                      )}
                    >
                      {entry.is_dir ? (
                        <Folder className="size-4 text-accent" aria-hidden />
                      ) : (
                        <FileText className="size-4" aria-hidden />
                      )}
                      <span className="truncate">{entry.name}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* footer: validity + actions */}
          <div className="flex items-center justify-between gap-inline border-t border-hairline px-body py-inline">
            <div
              data-storage-validation={status}
              className="flex items-center gap-hair font-ui text-[0.78rem]"
            >
              {statusInfo.ok ? (
                <>
                  <CheckCircle2 className="size-3.5 text-supported" aria-hidden />
                  <span className="text-supported">{statusInfo.text}</span>
                </>
              ) : (
                <>
                  <XCircle className="size-3.5 text-weak" aria-hidden />
                  <span className="text-weak">{statusInfo.text}</span>
                </>
              )}
            </div>
            <div className="flex items-center gap-inline">
              <Dialog.Close asChild>
                <button
                  type="button"
                  className="rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text"
                >
                  Cancel
                </button>
              </Dialog.Close>
              <button
                type="button"
                data-disco-control="settings.storage-pick"
                onClick={confirm}
                disabled={!data || data.selectable !== "ok"}
                className="rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                Choose this directory
              </button>
            </div>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

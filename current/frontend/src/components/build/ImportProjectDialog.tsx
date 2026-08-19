import * as Dialog from "@radix-ui/react-dialog";
import { useQueryClient } from "@tanstack/react-query";
import { FileArchive, FolderOpen, GitBranch, Loader2, X } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { importProject } from "@/api/projects";
import { agentIsLive } from "@/api/liveness";
import { PROJECTS_KEY } from "@/hooks/useProjects";
import { cn } from "@/lib/cn";
import type { ProjectImportInput } from "@/types/project";

type ImportTab = "zip" | "path" | "git";

const TABS: { id: ImportTab; label: string; icon: LucideIcon }[] = [
  { id: "zip", label: "Upload zip", icon: FileArchive },
  { id: "path", label: "Local folder path", icon: FolderOpen },
  { id: "git", label: "Git URL", icon: GitBranch },
];

export function ImportProjectDialog() {
  const live = agentIsLive();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<ImportTab>("zip");
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [localPath, setLocalPath] = useState("");
  const [gitUrl, setGitUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const input = useMemo<ProjectImportInput | null>(() => {
    if (tab === "zip") return zipFile ? { kind: "zip", file: zipFile } : null;
    if (tab === "path") {
      const path = localPath.trim();
      return path ? { kind: "path", path } : null;
    }
    const url = gitUrl.trim();
    return url ? { kind: "git", gitUrl: url } : null;
  }, [gitUrl, localPath, tab, zipFile]);

  const canSubmit = live && input !== null && !busy;

  async function submit() {
    if (!input || !live) return;
    setBusy(true);
    setError(null);
    try {
      const result = await importProject(input);
      await queryClient.invalidateQueries({ queryKey: PROJECTS_KEY });
      setOpen(false);
      navigate(`/build/${result.conversation_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function selectTab(next: ImportTab) {
    setTab(next);
    setError(null);
  }

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button
          type="button"
          disabled={!live}
          aria-label="Import project"
          title={live ? "Import project" : "Project import requires a live agent server"}
          data-disco-control="build.import-project"
          className="flex min-h-11 items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text disabled:opacity-40 lg:min-h-0"
        >
          <FolderOpen className="size-3.5" aria-hidden />
          Import project
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex max-h-[88vh] w-[min(34rem,92vw)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-card border border-hairline bg-bg p-body pmx-rise">
          <div className="flex items-start justify-between gap-inline">
            <div>
              <Dialog.Title className="font-ui text-[0.95rem] font-semibold text-text">
                Import project
              </Dialog.Title>
              <Dialog.Description className="sr-only">
                Import a zip, local folder, or git repository into a new Build conversation.
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <button
                type="button"
                aria-label="Close import project"
                className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-control p-hair text-text-faint transition-colors hover:text-text lg:min-h-0 lg:min-w-0"
              >
                <X className="size-4" aria-hidden />
              </button>
            </Dialog.Close>
          </div>

          <div
            className="mt-section grid grid-cols-3 rounded-control border border-hairline bg-surface-1 p-px"
            role="tablist"
            aria-label="Import source"
          >
            {TABS.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                onClick={() => selectTab(id)}
                className={cn(
                  "flex min-h-11 min-w-0 items-center justify-center gap-hair rounded-control px-hair py-hair font-ui text-[0.76rem] transition-colors lg:min-h-0",
                  tab === id
                    ? "bg-bg text-text shadow-sm"
                    : "text-text-muted hover:text-text",
                )}
              >
                <Icon className="size-3.5 shrink-0" aria-hidden />
                <span className="truncate">{label}</span>
              </button>
            ))}
          </div>

          <div className="mt-section">
            {tab === "zip" && (
              <label className="flex flex-col gap-hair font-ui text-[0.78rem] text-text-muted">
                Zip file
                <input
                  type="file"
                  accept=".zip,application/zip"
                  aria-label="Project zip file"
                  disabled={busy}
                  onChange={(event) => {
                    setZipFile(event.target.files?.[0] ?? null);
                    setError(null);
                  }}
                  className="min-h-11 rounded-control border border-hairline bg-surface-1 px-inline py-hair text-text file:mr-inline file:rounded-control file:border-0 file:bg-accent file:px-inline file:py-hair file:font-ui file:text-bg lg:min-h-0"
                />
              </label>
            )}

            {tab === "path" && (
              <label className="flex flex-col gap-hair font-ui text-[0.78rem] text-text-muted">
                Absolute folder path
                <input
                  type="text"
                  value={localPath}
                  disabled={busy}
                  onChange={(event) => {
                    setLocalPath(event.target.value);
                    setError(null);
                  }}
                  placeholder="/absolute/path/to/project"
                  aria-label="Local folder path"
                  className="min-h-11 rounded-control border border-hairline bg-surface-1 px-inline py-hair font-mono text-[0.82rem] text-text outline-none transition-colors focus:border-hairline-strong lg:min-h-0"
                />
              </label>
            )}

            {tab === "git" && (
              <label className="flex flex-col gap-hair font-ui text-[0.78rem] text-text-muted">
                Git URL
                <input
                  type="url"
                  value={gitUrl}
                  disabled={busy}
                  onChange={(event) => {
                    setGitUrl(event.target.value);
                    setError(null);
                  }}
                  placeholder="https://github.com/org/repo.git"
                  aria-label="Git URL"
                  className="min-h-11 rounded-control border border-hairline bg-surface-1 px-inline py-hair font-mono text-[0.82rem] text-text outline-none transition-colors focus:border-hairline-strong lg:min-h-0"
                />
              </label>
            )}
          </div>

          {error && (
            <p role="alert" className="mt-inline font-ui text-[0.78rem] leading-snug text-unsupported">
              {error}
            </p>
          )}

          <div className="mt-section flex justify-end gap-inline">
            <Dialog.Close asChild>
              <button
                type="button"
                disabled={busy}
                className="min-h-11 rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text disabled:opacity-40 lg:min-h-0"
              >
                Cancel
              </button>
            </Dialog.Close>
            <button
              type="button"
              disabled={!canSubmit}
              onClick={() => void submit()}
              data-disco-control="build.import-project.submit"
              className="flex min-h-11 items-center gap-hair rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-45 lg:min-h-0"
            >
              {busy && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
              Import
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

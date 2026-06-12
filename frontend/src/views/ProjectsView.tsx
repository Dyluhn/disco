/**
 * Projects — the persistent Build-workspace library, a SEPARATE surface from
 * the search/conversation History (different kind of thing: ephemeral research
 * vs. resumable build workspaces). Reuses the History surface's list-and-reopen
 * pattern (the same skeleton + empty + error states; same delete-with-confirm)
 * but renders project-specific columns (file count, last-snapshot, the
 * files-missing badge that surfaces graceful-failure honestly).
 */

import {
  AlertCircle,
  AlertTriangle,
  Download,
  FolderGit2,
  Search,
  Settings as SettingsIcon,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { formatDate } from "@/lib/date";
import { useDeleteProject, useDownloadProject, useProjects } from "@/hooks/useProjects";
import type { Project, ProjectStorageStatus } from "@/types/project";

function SkeletonRows() {
  return (
    <ul aria-hidden className="flex flex-col">
      {[0, 1, 2, 3].map((i) => (
        <li
          key={i}
          className="flex flex-col gap-hair border-b border-hairline py-body last:border-b-0"
        >
          <span className="h-3 w-2/3 animate-pulse rounded-full bg-surface-2" />
          <span className="h-2 w-24 animate-pulse rounded-full bg-surface-2" />
        </li>
      ))}
    </ul>
  );
}

function ConfigureStorage({ reason, path }: { reason: ProjectStorageStatus; path?: string }) {
  const message: Record<ProjectStorageStatus, string> = {
    ok: "",
    unset: "No project storage configured. Choose a directory in Settings to begin saving Build workspaces.",
    not_found: `Project storage directory ${path ? `\`${path}\` ` : ""}not found — it may have been moved or deleted. Re-select it in Settings.`,
    not_a_directory: `${path ? `\`${path}\` ` : "The configured path "}is not a directory. Re-select it in Settings.`,
    not_writable: `${path ? `\`${path}\` ` : "The configured path "}is not writable. Pick a different directory in Settings.`,
  };
  return (
    <div className="flex flex-col items-center gap-inline rounded-card border border-hairline bg-surface-1 p-major text-center">
      <FolderGit2 className="size-6 text-text-faint" aria-hidden />
      <div className="font-ui text-[0.95rem] font-medium text-text">
        {reason === "unset" ? "No project storage configured" : "Project storage unavailable"}
      </div>
      <p className="max-w-measure font-ui text-[0.85rem] text-text-muted">{message[reason]}</p>
      <Link
        to="/settings"
        className="mt-inline flex items-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:text-text"
      >
        <SettingsIcon className="size-3.5" aria-hidden /> Open Settings
      </Link>
    </div>
  );
}

function EmptyProjects() {
  const navigate = useNavigate();
  return (
    <div className="flex flex-col items-center gap-inline rounded-card border border-hairline bg-surface-1 p-major text-center">
      <FolderGit2 className="size-6 text-text-faint" aria-hidden />
      <div className="font-ui text-[0.95rem] font-medium text-text">No projects yet</div>
      <p className="max-w-measure font-ui text-[0.85rem] text-text-muted">
        Build a project and the workspace will be saved here automatically. You can reopen it later
        and pick up where you left off.
      </p>
      <button
        type="button"
        onClick={() => navigate("/")}
        className="mt-inline rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:text-text"
      >
        Start a build
      </button>
    </div>
  );
}

function ProjectRow({ project, onDelete }: { project: Project; onDelete: () => void }) {
  const navigate = useNavigate();
  const download = useDownloadProject();
  return (
    <li className="flex items-center justify-between gap-section border-b border-hairline py-inline last:border-b-0">
      <button
        type="button"
        onClick={() =>
          navigate(project.surface === "agent" ? `/agent/${project.id}` : `/build/${project.id}`)
        }
        className="group min-w-0 flex-1 text-left"
      >
        <div className="flex items-center gap-inline">
          <span className="truncate font-ui text-[0.9rem] text-text transition-colors group-hover:text-accent">
            {project.title}
          </span>
          {project.files_missing && (
            <span className="flex shrink-0 items-center gap-hair rounded-full border border-unsupported/60 px-inline py-px font-ui text-[0.66rem] uppercase tracking-wide text-unsupported">
              <AlertCircle className="size-3" aria-hidden />
              files missing
            </span>
          )}
        </div>
        <div className="font-ui text-[0.76rem] text-text-faint">
          {project.last_snapshot_at
            ? `Last saved ${formatDate(project.last_snapshot_at)}`
            : project.created_at
              ? `Created ${formatDate(project.created_at)}`
              : "—"}
          {!project.files_missing && (
            <>
              {" · "}
              {project.file_count} {project.file_count === 1 ? "file" : "files"}
            </>
          )}
        </div>
      </button>
      <div className="flex shrink-0 items-center gap-hair">
        <button
          type="button"
          onClick={() => download.mutate(project.id)}
          disabled={project.files_missing || download.isPending}
          aria-label={`Download project: ${project.title}`}
          title={project.files_missing ? "Files missing — nothing to download" : "Download a zip"}
          className="grid size-8 place-items-center rounded-control border border-hairline text-text-faint transition-colors hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Download className="size-4" aria-hidden />
        </button>
        <ConfirmDialog
          title="Delete this project?"
          description="This removes the saved workspace files. The conversation history is left intact in History."
          confirmLabel="Delete"
          onConfirm={onDelete}
          trigger={
            <button
              type="button"
              aria-label={`Delete project: ${project.title}`}
              className="grid size-8 place-items-center rounded-control border border-hairline text-text-faint transition-colors hover:border-unsupported/50 hover:text-unsupported"
            >
              <Trash2 className="size-4" aria-hidden />
            </button>
          }
        />
      </div>
    </li>
  );
}

export function ProjectsView() {
  const { data, isLoading, isError, refetch } = useProjects();
  const del = useDeleteProject();
  const [q, setQ] = useState("");

  const status = data?.status ?? "unset";
  const all = data?.projects ?? [];
  const filtered = all.filter((p) => p.title.toLowerCase().includes(q.trim().toLowerCase()));

  return (
    <div className="mx-auto w-full max-w-doc px-body py-section">
      <div className="mx-auto flex w-full max-w-[46rem] flex-col gap-section">
        <header>
          <h1 className="font-display text-[2rem] tracking-tight text-text">Projects</h1>
          <p className="font-ui text-[0.88rem] text-text-muted">
            Build workspaces saved to your machine — reopen one to continue with the agent on the
            same files.
          </p>
        </header>

        {!isLoading && !isError && status !== "ok" && (
          <ConfigureStorage reason={status} path={data?.root} />
        )}

        {!isLoading && !isError && status === "ok" && all.length > 0 && (
          <div className="flex items-center gap-inline rounded-control border border-hairline bg-surface-1 px-inline py-hair focus-within:border-hairline-strong">
            <Search className="size-4 shrink-0 text-text-faint" aria-hidden />
            <input
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search projects…"
              aria-label="Search projects"
              className="w-full bg-transparent font-ui text-[0.88rem] text-text outline-none placeholder:text-text-faint"
            />
          </div>
        )}

        {isLoading && <SkeletonRows />}

        {isError && (
          <div
            role="alert"
            className="flex flex-col items-center gap-inline rounded-card border border-hairline border-l-2 border-l-warn bg-surface-1 p-body text-center"
          >
            <AlertTriangle className="size-5 text-warn" aria-hidden />
            <div className="font-ui text-[0.88rem] text-text">Couldn't load your projects.</div>
            <button
              type="button"
              onClick={() => refetch()}
              className="rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text"
            >
              Try again
            </button>
          </div>
        )}

        {!isLoading && !isError && status === "ok" && all.length === 0 && <EmptyProjects />}

        {!isLoading && !isError && status === "ok" && all.length > 0 && (
          <>
            {filtered.length === 0 ? (
              <p className="py-body font-ui text-[0.85rem] text-text-muted">
                No projects match “{q}”.
              </p>
            ) : (
              <ul className="flex flex-col">
                {filtered.map((p) => (
                  <ProjectRow key={p.id} project={p} onDelete={() => del.mutate(p.id)} />
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </div>
  );
}

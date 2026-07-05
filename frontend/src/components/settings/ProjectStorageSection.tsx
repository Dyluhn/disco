/**
 * Settings → Project Storage. The user-chosen, server-side directory where
 * Build projects persist. Mirrors `SandboxSection`'s draft+dirty+save shape;
 * the new piece is the inline server-side directory PICKER (via PathPicker
 * Dialog) — the path is settings on the APP host, not the browser machine.
 *
 * Live validity (ok / not_found / not_a_directory / not_writable) renders
 * below the input so a bad path is named before save.
 */

import { CheckCircle2, FolderSearch, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { PathPickerDialog } from "@/components/PathPickerDialog";
import { cn } from "@/lib/cn";
import {
  useProjectsConfig,
  useUpdateProjectsConfig,
} from "@/hooks/useProjectsConfig";
import type { ProjectStorageStatus } from "@/types/project";

const STATUS_HELP: Record<
  ProjectStorageStatus,
  { ok: boolean; label: string; help: string }
> = {
  ok: { ok: true, label: "Valid", help: "Projects will persist here." },
  unset: {
    ok: false,
    label: "Unset",
    help: "Builds will not be saved until you choose a directory.",
  },
  not_found: {
    ok: false,
    label: "Not found",
    help: "The configured directory doesn't exist (it may have been deleted or moved).",
  },
  not_a_directory: {
    ok: false,
    label: "Not a directory",
    help: "The configured path points to a file, not a folder.",
  },
  not_writable: {
    ok: false,
    label: "Not writable",
    help: "The configured directory isn't writable by the app process.",
  },
};

export function ProjectStorageSection() {
  const { data, isLoading } = useProjectsConfig();
  const save = useUpdateProjectsConfig();
  const [draft, setDraft] = useState<string>("");

  useEffect(() => {
    if (data) setDraft(data.projects_root);
  }, [data]);

  if (isLoading || !data) {
    return (
      <section className="flex flex-col gap-inline">
        <h3 className="font-ui text-[0.95rem] font-semibold text-text">
          Project storage
        </h3>
        <p className="font-ui text-[0.86rem] text-text-faint">Loading…</p>
      </section>
    );
  }

  const dirty = draft.trim() !== data.projects_root.trim();
  const status = data.status;
  const help = STATUS_HELP[status];

  // e4ux: when the user hasn't set a path, surface the AUTO-CREATED default
  // the server will actually use. The input stays empty (placeholder visible)
  // so the user can override; the "(default)" marker makes it transparent that
  // the shown path is server-chosen, not user-typed. When projects_root IS
  // set, behave as before — show the input value as the active location.
  const usingDefault = data.projects_root.trim() === "";
  const activeRoot = usingDefault ? data.effective_root : data.projects_root;

  // Surface any 400/JSON error reason returned by the server in the save error.
  const saveErrorMsg = (() => {
    if (!save.error) return null;
    const raw = (save.error as Error).message;
    try {
      const j = JSON.parse(raw);
      const reason = j?.detail?.reason ?? j?.reason;
      if (reason)
        return STATUS_HELP[reason as ProjectStorageStatus]?.help ?? reason;
    } catch {
      /* not JSON — fall through */
    }
    return raw;
  })();

  return (
    <section className="flex flex-col gap-inline">
      <header>
        <h3 className="font-ui text-[0.95rem] font-semibold text-text">
          Project storage
        </h3>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Where Build projects persist on the app host (a directory you choose).
          Saved workspaces let you reopen a project later with its files
          restored, and export them as a zip.
        </p>
      </header>

      <div className="flex flex-col gap-hair">
        <label
          className="font-ui text-[0.78rem] text-text-muted"
          htmlFor="projects-root"
        >
          Projects root
        </label>
        <div className="flex items-center gap-inline">
          <input
            id="projects-root"
            value={draft}
            placeholder="/home/you/disco-projects"
            spellCheck={false}
            onChange={(e) => setDraft(e.target.value)}
            className="flex-1 rounded-control border border-hairline bg-surface-1 px-inline py-hair font-mono text-[0.82rem] text-text outline-none transition-colors focus:border-hairline-strong"
          />
          <PathPickerDialog
            initialPath={draft}
            onChoose={(p) => setDraft(p)}
            trigger={
              <button
                type="button"
                className="flex items-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text"
              >
                <FolderSearch className="size-3.5" aria-hidden />
                Browse…
              </button>
            }
          />
        </div>
        <p
          data-storage-validation={status}
          className={cn(
            "flex items-center gap-hair font-ui text-[0.78rem]",
            help.ok
              ? "text-supported"
              : status === "unset"
                ? "text-text-faint"
                : "text-weak",
          )}
        >
          {help.ok ? (
            <CheckCircle2 className="size-3.5" aria-hidden />
          ) : (
            <XCircle className="size-3.5" aria-hidden />
          )}
          {help.label} — {help.help}
        </p>
        {activeRoot && (
          <p
            data-testid="projects-active-root"
            className="font-ui text-[0.78rem] text-text-muted"
          >
            Saving to: <span className="font-mono text-text">{activeRoot}</span>
            {usingDefault && (
              <span
                data-testid="projects-default-marker"
                className="ml-hair rounded-pill border border-hairline px-hair py-[1px] font-ui text-[0.7rem] text-text-faint"
              >
                (default)
              </span>
            )}
          </p>
        )}
      </div>

      <div className="flex items-center gap-inline">
        <button
          type="button"
          data-disco-control="settings.storage-save"
          onClick={() =>
            save.mutate({ projects_root: draft.trim(), status: "unset" })
          }
          disabled={!dirty || save.isPending}
          className="rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {save.isPending ? "Saving…" : dirty ? "Save path" : "Saved"}
        </button>
        {saveErrorMsg && (
          <span
            role="alert"
            className="font-ui text-[0.78rem] text-unsupported"
          >
            {saveErrorMsg}
          </span>
        )}
      </div>
    </section>
  );
}

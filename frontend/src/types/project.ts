/**
 * Build projects — persistent conversations + their workspaces, listed and
 * reopenable from the Projects surface. A separate kind of thing from search
 * History (ephemeral research conversations); the Projects view mirrors
 * History's list-and-reopen pattern but renders project-specific columns.
 */

/** The validation classification of a candidate `projects_root`. Mirrors the
 * Python `StorageStatus` enum string values. */
export type ProjectStorageStatus =
  | "ok"
  | "unset"
  | "not_found"
  | "not_a_directory"
  | "not_writable";

export interface Project {
  id: string; // = conversation_id (1:1)
  owner_id: string;
  title: string;
  created_at: string | null;
  last_snapshot_at: string | null;
  file_count: number;
  total_bytes: number;
  /** True when the manifest exists but the workspace tree is gone (deleted
   * under the app — the graceful-failure flag the list view surfaces). */
  files_missing: boolean;
  /** The surface that produced it — the list resumes "agent" projects on /agent/:cid
   * and everything else on /build/:cid. Defaults to "build" server-side. */
  surface?: "build" | "agent";
}

export interface ProjectsList {
  projects: Project[];
  status: ProjectStorageStatus;
  /** The configured projects_root, even when invalid — so the UI can name the
   * exact path in the error state ("project storage directory `<path>` not
   * found"). Empty string when unset. */
  root?: string;
}

export interface ProjectImportResult {
  conversation_id: string;
  files: number;
  bytes: number;
  title: string;
}

export type ProjectImportInput =
  | { kind: "zip"; file: File }
  | { kind: "path"; path: string }
  | { kind: "git"; gitUrl: string };

/** The wire shape for the Settings → Project Storage section. `status` is
 * derived server-side on GET (so the UI shows live validity, not last-saved).
 *
 * `effective_root` is the REAL directory currently in use — auto-created when
 * `projects_root` is empty (the zero-config default) and equal to
 * `projects_root` when the user has set one. Lets the UI show "Saving to:
 * <path> (default)" so a fresh user can see WHERE builds are saved without
 * forcing them to type a path. `projects_root` stays as the raw configured
 * value ("" = "use the auto default") so the explicit-vs-default distinction
 * is preserved across round-trips. */
export interface ProjectStorageConfig {
  projects_root: string;
  status: ProjectStorageStatus;
  effective_root: string;
}

/** The SAVE request shape (PUT /api/projects/storage/config). The client only
 * supplies `projects_root`; `status` and `effective_root` are derived
 * server-side and returned on the response, so the client never invents them.
 * (`Omit` keeps it in lockstep with `ProjectStorageConfig`.) */
export type ProjectStorageSaveInput = Omit<ProjectStorageConfig, "effective_root">;

/** A row in the server-side directory picker — directory or file flag only;
 * the picker never returns file contents. */
export interface BrowseEntry {
  name: string;
  is_dir: boolean;
}

/** The picker's listing for a single requested directory. */
export interface BrowseResult {
  path: string;
  parent: string | null;
  entries: BrowseEntry[];
  /** Whether `path` itself is selectable as a projects_root (`ok` = yes;
   * everything else = the named reason). Lets the picker show inline
   * confirmation / a clear blocker on Choose. */
  selectable: ProjectStorageStatus;
}

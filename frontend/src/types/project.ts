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
}

export interface ProjectsList {
  projects: Project[];
  status: ProjectStorageStatus;
  /** The configured projects_root, even when invalid — so the UI can name the
   * exact path in the error state ("project storage directory `<path>` not
   * found"). Empty string when unset. */
  root?: string;
}

/** The wire shape for the Settings → Project Storage section. `status` is
 * derived server-side on GET (so the UI shows live validity, not last-saved). */
export interface ProjectStorageConfig {
  projects_root: string;
  status: ProjectStorageStatus;
}

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

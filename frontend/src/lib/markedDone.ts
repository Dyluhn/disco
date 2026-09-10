/**
 * UI-41 — remembering that the user pressed "Mark done" on a finished build.
 *
 * WHY THIS IS LOCAL AND NOT SERVER STATE. The `accept_finished` frame is
 * authoritative stop-intent, but on an already-FINISHED conversation the server
 * deliberately records NOTHING for it: `ControlOps.accept_finished` appends an
 * event only when the frame carries a user note, and the UI's button sends no
 * note (see `packages/agent-server/.../control_ops.py` and its regression
 * `test_accept_finished_stays_finished_no_events_without_text`). So there is no
 * server-side acceptance flag to read back, and inventing one would mean either a
 * new persisted field or writing a fake "Marked done" user turn into the model's
 * transcript. Neither is warranted for what this control actually is: the user's
 * own acknowledgement that they are done looking at this build.
 *
 * So it is stored per conversation in `localStorage`, the same way the Build
 * surface already persists per-viewer UI state (`ResizableSplit`,
 * `SelectionOverlay`). Every access is guarded: a private window, cleared site
 * data, or a browser blocking site storage must degrade to "not marked", never
 * throw.
 */

const KEY_PREFIX = "disco.build.marked-done.";

function keyFor(cid: string): string {
  return `${KEY_PREFIX}${cid}`;
}

/** Has this conversation been marked done in this browser? False for a null cid,
 *  and false whenever storage is unavailable. */
export function isMarkedDone(cid: string | null): boolean {
  if (!cid) return false;
  try {
    return window.localStorage.getItem(keyFor(cid)) === "1";
  } catch {
    return false;
  }
}

/** Record the acknowledgement so it survives a reload. Silently a no-op when
 *  storage is unavailable — the in-session button state still updates. */
export function rememberMarkedDone(cid: string | null): void {
  if (!cid) return;
  try {
    window.localStorage.setItem(keyFor(cid), "1");
  } catch {
    /* storage unavailable — the acknowledgement is session-only */
  }
}

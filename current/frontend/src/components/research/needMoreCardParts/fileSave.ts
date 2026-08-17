/**
 * NeedMoreCard — File System Access API save path + anchor-download fallback
 * (extracted from NeedMoreCard.tsx; `hasFSA`/`saveViaPicker`/the FSA types are
 * moved verbatim, `downloadBlob` factors out the repeated anchor-download
 * snippet from the export + audio-export code paths).
 */

// ── File System Access API types ──────────────────────────────────────────────

export interface SaveFilePickerOptions {
  suggestedName?: string;
  types?: Array<{ description?: string; accept: Record<string, string[]> }>;
}

interface FSAWindow {
  showSaveFilePicker: (opts?: SaveFilePickerOptions) => Promise<FileSystemFileHandle>;
}

export function hasFSA(): boolean {
  return (
    typeof window !== "undefined" &&
    "showSaveFilePicker" in window
  );
}

export async function saveViaPicker(
  blob: Blob,
  opts: SaveFilePickerOptions,
): Promise<void> {
  const win = window as unknown as FSAWindow;
  const fh = await win.showSaveFilePicker(opts);
  const writable = await fh.createWritable();
  await writable.write(blob);
  await writable.close();
}

/** Anchor-download fallback for browsers without the File System Access API
 * (and for the "Export MP3" affordance, which always uses it). */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

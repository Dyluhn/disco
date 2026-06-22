/**
 * EditAffordance — A1.4 inline click-to-edit box.
 *
 * Rendered over the editable preview iframe when the user clicks an element that
 * resolves to a real stamped source `file:line` (a `kind:'source'` selection with
 * a non-blank `data-oid`). The user types an instruction and Applies it; the host
 * forms `In {file} near line {line}, {instruction}` and steers the agent.
 *
 * No false affordance: this component is only mounted by PreviewPane for a real
 * source selection — a deck / non-source / blank-oid selection never renders it.
 * The box is positioned just under the selected element's rect (same coordinate
 * space as SelectionOverlay's label bar).
 */

import { useEffect, useRef, useState } from "react";

interface Props {
  /** Workspace-relative source file of the clicked element. */
  file: string;
  /** 1-based source line of the clicked element. */
  line: number;
  /** Selected element rect (iframe viewport coords) — for positioning. */
  rect: { x: number; y: number; width: number; height: number };
  /** Apply the edit: the parent forms the steer instruction + sends it. */
  onApply: (instruction: string) => void;
  /** Dismiss without editing. */
  onCancel: () => void;
}

export function EditAffordance({ file, line, rect, onApply, onCancel }: Props) {
  const [value, setValue] = useState("");
  const ref = useRef<HTMLTextAreaElement | null>(null);

  // Focus the box when it appears (so the user can type immediately).
  useEffect(() => {
    ref.current?.focus();
  }, []);

  // Position under the element; the label bar sits ~4px below, so offset a bit
  // more to clear it. Clamp left to the overlay area.
  const left = Math.max(0, rect.x);
  const top = rect.y + rect.height + 26;

  const trimmed = value.trim();

  return (
    <div
      className="absolute z-[103] flex w-[280px] max-w-[calc(100%-12px)] flex-col gap-hair rounded-control border border-accent bg-bg p-inline shadow-lg"
      style={{ left, top, pointerEvents: "auto" }}
    >
      <span className="truncate font-mono text-[0.64rem] text-text-faint" title={`${file}:${line}`}>
        Edit {file}:{line}
      </span>
      <textarea
        ref={ref}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          // Cmd/Ctrl+Enter applies; Escape cancels.
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && trimmed) {
            e.preventDefault();
            onApply(value);
          } else if (e.key === "Escape") {
            e.preventDefault();
            onCancel();
          }
        }}
        rows={2}
        placeholder="Describe the change (e.g. make this heading larger)…"
        className="resize-none rounded border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.76rem] text-text outline-none focus:border-accent"
      />
      <div className="flex items-center justify-end gap-hair">
        <button
          type="button"
          onClick={onCancel}
          className="rounded px-inline py-px font-ui text-[0.72rem] text-text-muted transition-colors hover:text-text"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={() => onApply(value)}
          disabled={!trimmed}
          aria-label="Apply this edit — steer the agent to change the source"
          data-disco-control="build.edit-apply"
          className="rounded bg-accent px-inline py-px font-ui text-[0.72rem] text-white transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          Apply
        </button>
      </div>
    </div>
  );
}

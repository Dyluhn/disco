/**
 * EditAffordance — P8 inline click-to-edit box.
 *
 * Rendered over the editable preview iframe when the user clicks an element in edit
 * mode. The user types an instruction and Applies it; the parent submits the clicked
 * element's TYPED ref + the instruction over the `selection_edit` wire, and the HOST
 * turns it into a precisely-anchored, targeted-edit directive (core/selection_edit.py)
 * — so the model changes only that element, never a rewrite.
 *
 * `targetLabel` is the human-readable element description (the selection agent's
 * `human_label`, e.g. `h1 — "Nightshift Coffee"`) shown so the user knows what they're
 * editing; the precise anchor travels in the ref, not this label.
 *
 * No false affordance: mounted only for a real selection in edit mode (where the
 * preview_edit route stamps the anchors). The box is positioned just under the
 * selected element's rect (same coordinate space as SelectionOverlay's label bar).
 */

import { useEffect, useRef, useState } from "react";

interface Props {
  /** Human-readable description of the clicked element (for display only). */
  targetLabel: string;
  /** Selected element rect (iframe viewport coords) — for positioning. */
  rect: { x: number; y: number; width: number; height: number };
  /** Apply the edit: the parent submits the ref + instruction over selection_edit. */
  onApply: (instruction: string) => void;
  /** Dismiss without editing. */
  onCancel: () => void;
}

export function EditAffordance({ targetLabel, rect, onApply, onCancel }: Props) {
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
      <span className="truncate font-mono text-[0.64rem] text-text-faint" title={targetLabel}>
        Edit {targetLabel}
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
          className="min-h-11 rounded px-inline py-px font-ui text-[0.72rem] text-text-muted transition-colors hover:text-text lg:min-h-0"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={() => onApply(value)}
          disabled={!trimmed}
          aria-label="Apply this edit — steer the agent to change the source"
          data-disco-control="build.edit-apply"
          className="min-h-11 rounded bg-accent px-inline py-px font-ui text-[0.72rem] text-white transition-opacity hover:opacity-90 disabled:opacity-40 lg:min-h-0"
        >
          Apply
        </button>
      </div>
    </div>
  );
}

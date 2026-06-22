/**
 * ElementBox — a transparent click-to-edit overlay positioned over the WYSIWYG iframe.
 *
 * In the real-render overlay model (§5d), ElementBox is NOT a visual element — the
 * iframe shows the actual slide visual. ElementBox is a TRANSPARENT positioned box whose
 * only jobs are:
 *  1. Capture a click → select (highlights in LayersPanel, shows info bar).
 *  2. Capture a double-click on text-editable kinds → open an editable input OVER the
 *     iframe text → commit → emit a RFC-6902 `replace` patch via `onPatch`.
 *
 * Position comes from the measured `rect` (px from the iframe container), not from the
 * old `%`-geometry. No drag (the AuthoredDeck schema has no element positions). No
 * always-on dashed outline — only a selection ring when `selected`.
 *
 * Keyboard a11y: `tabIndex=0`, Enter/Space to select, double-Enter or double-click to
 * edit when the element is already selected.
 *
 * @module ElementBox
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { JsonPatchOp, LoweredElement } from "./types";

// ─── Props ───────────────────────────────────────────────────────────────────

interface ElementBoxProps {
  /** e.g. "slide-0:title" */
  elementId: string;
  /** RFC-6901 path into AuthoredDeck — used in patch ops */
  jsonPointer: string;
  kind: LoweredElement["kind"];
  /** Current text content (from the LoweredDeck) — initialises the edit input */
  content: string;
  /** Measured position in px relative to the iframe container */
  rect: { left: number; top: number; width: number; height: number };
  selected: boolean;
  onSelect: (elementId: string) => void;
  onPatch: (patch: JsonPatchOp[]) => void;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function isTextEditable(kind: LoweredElement["kind"]): boolean {
  return kind === "title" || kind === "subtitle" || kind === "bullet";
}

// ─── ElementBox ──────────────────────────────────────────────────────────────

export function ElementBox({
  elementId,
  jsonPointer,
  kind,
  content,
  rect,
  selected,
  onSelect,
  onPatch,
}: ElementBoxProps) {
  const editable = isTextEditable(kind);
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(content);
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);

  // Keep editValue in sync when content changes externally (after a patch round-trip).
  useEffect(() => {
    if (!editing) setEditValue(content);
  }, [content, editing]);

  // Focus the input when entering edit mode.
  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      if (inputRef.current instanceof HTMLInputElement) {
        inputRef.current.select();
      }
    }
  }, [editing]);

  // ── Event handlers ──────────────────────────────────────────────────────────

  const handleClick = useCallback(
    (e: React.MouseEvent) => {
      if (editing) return;
      e.stopPropagation();
      onSelect(elementId);
    },
    [editing, elementId, onSelect],
  );

  const handleDoubleClick = useCallback(
    (e: React.MouseEvent) => {
      if (!editable) return;
      e.stopPropagation();
      onSelect(elementId);
      setEditing(true);
    },
    [editable, elementId, onSelect],
  );

  const commitEdit = useCallback(() => {
    setEditing(false);
    const trimmed = editValue.trim();
    if (trimmed !== content.trim()) {
      onPatch([{ op: "replace" as const, path: jsonPointer, value: trimmed }]);
    }
  }, [editValue, content, jsonPointer, onPatch]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        commitEdit();
      }
      if (e.key === "Escape") {
        setEditing(false);
        setEditValue(content); // revert
      }
    },
    [commitEdit, content],
  );

  // Keyboard a11y on the outer box (select / enter edit on space/enter).
  const handleBoxKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (editing) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (selected && editable) {
          setEditing(true);
        } else {
          onSelect(elementId);
        }
      }
    },
    [editing, selected, editable, elementId, onSelect],
  );

  // ── Styles ──────────────────────────────────────────────────────────────────

  const boxStyle: React.CSSProperties = {
    position: "absolute",
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height,
    boxSizing: "border-box",
    background: "transparent",
    pointerEvents: "auto",
    cursor: editable ? "text" : "pointer",
    // Selection outline — ONLY when selected, no always-on dashed outline.
    outline: selected ? "2px solid rgba(99,102,241,0.85)" : "none",
    outlineOffset: "1px",
  };

  return (
    <div
      data-element-id={elementId}
      // slide_id is the prefix of element_id (e.g. "slide-0:title" → "slide-0")
      data-slide-id={elementId.split(":")[0]}
      data-disco-control="build.deck-element"
      style={boxStyle}
      tabIndex={0}
      role="button"
      aria-label={`${kind}: ${content.slice(0, 60)}`}
      aria-pressed={selected}
      onClick={handleClick}
      onDoubleClick={handleDoubleClick}
      onKeyDown={handleBoxKeyDown}
    >
      {editing && editable && (
        kind === "bullet" ? (
          <textarea
            ref={inputRef as React.RefObject<HTMLTextAreaElement>}
            value={editValue}
            data-disco-control="build.deck-element-edit"
            aria-label={`Edit ${kind}`}
            onChange={(e) => setEditValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onBlur={commitEdit}
            style={{
              position: "absolute",
              inset: 0,
              width: "100%",
              height: "100%",
              border: "none",
              background: "rgba(255,255,255,0.92)",
              resize: "none",
              fontFamily: "inherit",
              fontSize: "inherit",
              outline: "2px solid #6366f1",
              padding: "2px 4px",
              boxSizing: "border-box",
              zIndex: 1,
            }}
          />
        ) : (
          <input
            ref={inputRef as React.RefObject<HTMLInputElement>}
            type="text"
            value={editValue}
            data-disco-control="build.deck-element-edit"
            aria-label={`Edit ${kind}`}
            onChange={(e) => setEditValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onBlur={commitEdit}
            style={{
              position: "absolute",
              inset: 0,
              width: "100%",
              height: "100%",
              border: "none",
              background: "rgba(255,255,255,0.92)",
              fontFamily: "inherit",
              fontSize: "inherit",
              outline: "2px solid #6366f1",
              padding: "2px 4px",
              boxSizing: "border-box",
              zIndex: 1,
            }}
          />
        )
      )}
    </div>
  );
}

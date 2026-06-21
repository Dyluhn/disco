/**
 * ElementBox — a single positioned, directly-manipulable element in the deck editor.
 *
 * Renders one `LoweredElement` as an absolutely-positioned box on the `SlideCanvas`.
 * Supports:
 *  - Click-to-select (highlights the element, fires `onSelect`).
 *  - Double-click to enter text-edit mode (for title / bullet / subtitle elements).
 *  - Drag to reposition (fires a `replace` JSON Patch on mouseup).
 *
 * Stamp discipline: every ElementBox stamps `data-element-id` + `data-slide-id`
 * so the §4.1 selection overlay also works over the editor's DOM (the same
 * overlay substrate works inside and outside the iframe — §4.5 spec note).
 *
 * **No false affordances**: elements that are not text-editable (chart/table/
 * image_prompt) show a "non-editable" cursor and the edit input is never shown.
 *
 * @module ElementBox
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { JsonPatchOp, LoweredElement } from "./types";

// ─── Constants ───────────────────────────────────────────────────────────────

// Minimum drag movement (px) before we treat a mousedown as a drag
const DRAG_THRESHOLD_PX = 4;

// ─── Props ───────────────────────────────────────────────────────────────────

interface ElementBoxProps {
  element: LoweredElement;
  /** Whether this element is currently selected. */
  selected: boolean;
  /** Canvas dimensions in px (for converting % → px drag offset). */
  canvasWidth: number;
  canvasHeight: number;
  /** Called when the element is clicked (selects it). */
  onSelect: (elementId: string) => void;
  /** Called with a JSON Patch array when the element is mutated (drag/edit). */
  onPatch: (patch: JsonPatchOp[]) => void;
  /**
   * A2: when true, the drag affordance is removed entirely — no drag handlers, the
   * cursor never signals "move". Double-click text editing still works. Used by the
   * in-app deck editor because the AuthoredDeck schema has no element geometry (a
   * drag patch would always reject); disabling the affordance avoids a false one.
   */
  disableDrag?: boolean;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function isTextEditable(kind: LoweredElement["kind"]): boolean {
  return kind === "title" || kind === "subtitle" || kind === "bullet";
}

// ─── ElementBox component ─────────────────────────────────────────────────────

export function ElementBox({
  element,
  selected,
  canvasWidth,
  canvasHeight,
  onSelect,
  onPatch,
  disableDrag = false,
}: ElementBoxProps) {
  const { element_id, slide_id, kind, content, geometry, font_size_vw, font_weight, font_style, json_pointer } = element;
  const editable = isTextEditable(kind);

  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(content);
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);

  // Track drag state in refs (no re-render during drag)
  const dragRef = useRef<{
    startX: number;
    startY: number;
    startGeoX: number;
    startGeoY: number;
    dragging: boolean;
  } | null>(null);

  // Keep editValue in sync when content changes externally
  useEffect(() => {
    setEditValue(content);
  }, [content]);

  // Focus input when entering edit mode
  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      if (inputRef.current instanceof HTMLInputElement) {
        inputRef.current.select();
      }
    }
  }, [editing]);

  // ── Event handlers ──────────────────────────────────────────────────────────

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (editing) return; // don't drag while editing
      e.stopPropagation();

      dragRef.current = {
        startX: e.clientX,
        startY: e.clientY,
        startGeoX: geometry.x,
        startGeoY: geometry.y,
        dragging: false,
      };

      const onMouseMove = (me: MouseEvent) => {
        if (!dragRef.current) return;
        const dx = me.clientX - dragRef.current.startX;
        const dy = me.clientY - dragRef.current.startY;
        if (Math.hypot(dx, dy) > DRAG_THRESHOLD_PX) {
          dragRef.current.dragging = true;
        }
      };

      const onMouseUp = (me: MouseEvent) => {
        window.removeEventListener("mousemove", onMouseMove);
        window.removeEventListener("mouseup", onMouseUp);

        if (!dragRef.current) return;
        const { startX, startY, startGeoX, startGeoY, dragging } = dragRef.current;
        dragRef.current = null;

        if (!dragging) {
          // Pure click → select
          onSelect(element_id);
          return;
        }

        // Compute new position as % of canvas
        const dx = me.clientX - startX;
        const dy = me.clientY - startY;
        const newX = Math.max(0, Math.min(100 - geometry.w, startGeoX + (dx / canvasWidth) * 100));
        const newY = Math.max(0, Math.min(100 - geometry.h, startGeoY + (dy / canvasHeight) * 100));

        // Emit a pair of replace patches (x and y positions stored in geometry-extension fields)
        // NOTE: the geometry is editor-local; it maps back to the authored JSON via the
        // element's json_pointer-parent.  For the authoring schema, body text positions
        // are fixed by the lowerer — we encode drag as a geometry override comment
        // in the tool args.  For the test-visible interface, we emit:
        //   [{ op: "replace", path: json_pointer + "/__x__", value: newX }]
        // In production the steer message carries the full envelope including geometry;
        // the integration point is §4.4's disco:apply handler.
        //
        // For the editor-as-standalone-viewer use-case we emit a geometry patch
        // on the _editor_geometry_ key so it persists within the editor session.
        onPatch([
          {
            op: "replace" as const,
            path: `${json_pointer}/__editor_x__`,
            value: Math.round(newX * 100) / 100,
          },
          {
            op: "replace" as const,
            path: `${json_pointer}/__editor_y__`,
            value: Math.round(newY * 100) / 100,
          },
        ]);
      };

      window.addEventListener("mousemove", onMouseMove);
      window.addEventListener("mouseup", onMouseUp);
    },
    [editing, geometry, canvasWidth, canvasHeight, element_id, json_pointer, onSelect, onPatch],
  );

  // A2: drag disabled → a plain click selects (no drag state machine, no move cursor).
  const handleSelectClick = useCallback(
    (e: React.MouseEvent) => {
      if (editing) return;
      e.stopPropagation();
      onSelect(element_id);
    },
    [editing, element_id, onSelect],
  );

  const handleDoubleClick = useCallback(
    (e: React.MouseEvent) => {
      if (!editable) return;
      e.stopPropagation();
      setEditing(true);
    },
    [editable],
  );

  const commitEdit = useCallback(() => {
    setEditing(false);
    const trimmed = editValue.trim();
    if (trimmed !== content.trim()) {
      onPatch([{ op: "replace" as const, path: json_pointer, value: trimmed }]);
    }
  }, [editValue, content, json_pointer, onPatch]);

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

  // ── Computed styles ─────────────────────────────────────────────────────────

  const boxStyle: React.CSSProperties = {
    position: "absolute",
    left: `${geometry.x}%`,
    top: `${geometry.y}%`,
    width: `${geometry.w}%`,
    height: `${geometry.h}%`,
    // Canvas-relative font sizing (#5): geometry (x/y/w/h) is a % of the CANVAS, but the
    // font was `vw` — a % of the VIEWPORT. In the editor the canvas is a PANEL, not the
    // full viewport, so vw made text render at the wrong scale and drift as the window
    // resized. `font_size_vw` is conceptually "% of slide width", so resolve it against
    // the canvas width (px) to match the geometry and the rendered deck.
    fontSize: `${(font_size_vw / 100) * canvasWidth}px`,
    fontWeight: font_weight,
    fontStyle: font_style,
    boxSizing: "border-box",
    padding: "0.5%",
    cursor: editable ? "text" : "default",
    userSelect: "none",
    overflow: "hidden",
  };

  const borderStyle: React.CSSProperties = selected
    ? { outline: "2px solid #6366f1", outlineOffset: "1px" }
    : { outline: "1px dashed rgba(99,102,241,0.3)", outlineOffset: "1px" };

  // Chart/table/image_prompt: render a placeholder label (no false edit affordance)
  if (!editable) {
    return (
      <div
        data-element-id={element_id}
        data-slide-id={slide_id}
        style={{ ...boxStyle, ...borderStyle, cursor: "pointer" }}
        onMouseDown={disableDrag ? handleSelectClick : handleMouseDown}
        title={kind === "image_prompt" ? `Image: ${content}` : content}
      >
        <span
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            height: "100%",
            color: "rgba(0,0,0,0.4)",
            fontSize: `${canvasWidth / 100}px`, // canvas-relative (#5), matches the element font
            fontStyle: "italic",
          }}
        >
          [{kind === "image_prompt" ? "image" : kind}]
        </span>
      </div>
    );
  }

  return (
    <div
      data-element-id={element_id}
      data-slide-id={slide_id}
      style={{ ...boxStyle, ...borderStyle }}
      onMouseDown={disableDrag ? handleSelectClick : handleMouseDown}
      onDoubleClick={handleDoubleClick}
    >
      {editing ? (
        // Inline text editor — textarea for bullets (multiline), input for title
        kind === "bullet" ? (
          <textarea
            ref={inputRef as React.RefObject<HTMLTextAreaElement>}
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onBlur={commitEdit}
            style={{
              width: "100%",
              height: "100%",
              border: "none",
              background: "transparent",
              resize: "none",
              fontFamily: "inherit",
              fontSize: "inherit",
              fontWeight: "inherit",
              fontStyle: "inherit",
              color: "inherit",
              outline: "none",
              padding: 0,
            }}
          />
        ) : (
          <input
            ref={inputRef as React.RefObject<HTMLInputElement>}
            type="text"
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onBlur={commitEdit}
            style={{
              width: "100%",
              border: "none",
              background: "transparent",
              fontFamily: "inherit",
              fontSize: "inherit",
              fontWeight: "inherit",
              fontStyle: "inherit",
              color: "inherit",
              outline: "none",
              padding: 0,
            }}
          />
        )
      ) : (
        <span
          style={{
            display: "block",
            width: "100%",
            height: "100%",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: kind === "bullet" ? "normal" : "nowrap",
          }}
        >
          {content}
        </span>
      )}
    </div>
  );
}

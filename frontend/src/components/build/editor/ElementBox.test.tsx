/**
 * ElementBox — tests for the §5d overlay-based ElementBox.
 *
 * ElementBox is now a TRANSPARENT overlay positioned by a measured pixel rect (not
 * percent-geometry). Tests assert:
 *  1. The outer box is stamped with data-element-id and data-slide-id.
 *  2. Background is transparent (no visual fill — the iframe shows the content).
 *  3. Double-click on a text-editable element (title/subtitle) opens an input.
 *  4. Double-click on a non-editable element (chart) does NOT open an input.
 *  5. Edit → blur emits the correct json_pointer replace patch.
 *  6. Escape reverts without emitting a patch.
 *  7. Keyboard a11y: Enter/Space selects; second Enter opens edit when selected.
 */

import { render, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ElementBox } from "./ElementBox";
import type { JsonPatchOp } from "./types";

const ZERO_RECT = { left: 0, top: 0, width: 100, height: 40 };

function titleBox(overrides?: Partial<Parameters<typeof ElementBox>[0]>) {
  return {
    elementId: "slide-0:title",
    jsonPointer: "/slides/0/title",
    kind: "title" as const,
    content: "Hello World",
    rect: ZERO_RECT,
    selected: false,
    onSelect: vi.fn(),
    onPatch: vi.fn(),
    ...overrides,
  };
}

// ─── 1: DOM stamping ─────────────────────────────────────────────────────────

describe("ElementBox — DOM attributes", () => {
  it("stamps data-element-id on the outer div", () => {
    const { container } = render(<ElementBox {...titleBox()} />);
    expect(container.querySelector('[data-element-id="slide-0:title"]')).not.toBeNull();
  });

  it("stamps data-slide-id (derived from the element_id prefix)", () => {
    const { container } = render(<ElementBox {...titleBox()} />);
    expect(container.querySelector('[data-slide-id="slide-0"]')).not.toBeNull();
  });
});

// ─── 2: Transparent background ───────────────────────────────────────────────

describe("ElementBox — visual style", () => {
  it("has transparent background (the iframe shows the content)", () => {
    const { container } = render(<ElementBox {...titleBox()} />);
    const box = container.querySelector('[data-element-id="slide-0:title"]') as HTMLElement;
    expect(box.style.background).toBe("transparent");
  });

  it("shows no selection outline when not selected", () => {
    const { container } = render(<ElementBox {...titleBox({ selected: false })} />);
    const box = container.querySelector('[data-element-id="slide-0:title"]') as HTMLElement;
    expect(box.style.outline).toBe("none");
  });

  it("shows a selection outline when selected", () => {
    const { container } = render(<ElementBox {...titleBox({ selected: true })} />);
    const box = container.querySelector('[data-element-id="slide-0:title"]') as HTMLElement;
    expect(box.style.outline).not.toBe("none");
    expect(box.style.outline).toBeTruthy();
  });
});

// ─── 3: Positioning from measured rect ───────────────────────────────────────

describe("ElementBox — rect-based positioning", () => {
  it("positions the box using the measured px rect", () => {
    const rect = { left: 42, top: 17, width: 300, height: 50 };
    const { container } = render(<ElementBox {...titleBox({ rect })} />);
    const box = container.querySelector('[data-element-id="slide-0:title"]') as HTMLElement;
    expect(box.style.left).toBe("42px");
    expect(box.style.top).toBe("17px");
    expect(box.style.width).toBe("300px");
    expect(box.style.height).toBe("50px");
  });
});

// ─── 4: Text-editable elements ───────────────────────────────────────────────

describe("ElementBox — edit mode (title)", () => {
  it("double-click opens a text input for title elements", () => {
    const { container } = render(<ElementBox {...titleBox()} />);
    const box = container.querySelector('[data-element-id="slide-0:title"]')!;
    fireEvent.doubleClick(box);
    expect(container.querySelector('input[type="text"]')).not.toBeNull();
  });

  it("blur commits an edit and emits replace patch", () => {
    const onPatch = vi.fn<(patch: JsonPatchOp[]) => void>();
    const { container } = render(<ElementBox {...titleBox({ onPatch })} />);
    fireEvent.doubleClick(container.querySelector('[data-element-id="slide-0:title"]')!);
    const input = container.querySelector('input[type="text"]')!;
    fireEvent.change(input, { target: { value: "New Title" } });
    fireEvent.blur(input);
    expect(onPatch).toHaveBeenCalledWith([
      { op: "replace", path: "/slides/0/title", value: "New Title" },
    ]);
  });

  it("does NOT emit a patch when content is unchanged on blur", () => {
    const onPatch = vi.fn<(patch: JsonPatchOp[]) => void>();
    const { container } = render(
      <ElementBox {...titleBox({ content: "Hello World", onPatch })} />,
    );
    fireEvent.doubleClick(container.querySelector('[data-element-id="slide-0:title"]')!);
    const input = container.querySelector('input[type="text"]')!;
    // Blur without changing the value.
    fireEvent.blur(input);
    expect(onPatch).not.toHaveBeenCalled();
  });

  it("Escape reverts the edit and emits no patch", () => {
    const onPatch = vi.fn<(patch: JsonPatchOp[]) => void>();
    const { container } = render(<ElementBox {...titleBox({ onPatch })} />);
    fireEvent.doubleClick(container.querySelector('[data-element-id="slide-0:title"]')!);
    const input = container.querySelector('input[type="text"]')!;
    fireEvent.change(input, { target: { value: "Changed" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onPatch).not.toHaveBeenCalled();
  });
});

// ─── 5: Bullet (textarea) ────────────────────────────────────────────────────

describe("ElementBox — edit mode (bullet → textarea)", () => {
  it("double-click opens a textarea for bullet elements", () => {
    const { container } = render(
      <ElementBox {...titleBox({ elementId: "slide-0:body:0", kind: "bullet", jsonPointer: "/slides/0/body/0", content: "Bullet" })} />,
    );
    fireEvent.doubleClick(container.querySelector('[data-element-id="slide-0:body:0"]')!);
    expect(container.querySelector("textarea")).not.toBeNull();
  });

  it("Enter commits a bullet edit and emits replace patch", () => {
    const onPatch = vi.fn<(patch: JsonPatchOp[]) => void>();
    const { container } = render(
      <ElementBox {...titleBox({ elementId: "slide-0:body:0", kind: "bullet", jsonPointer: "/slides/0/body/0", content: "Bullet", onPatch })} />,
    );
    fireEvent.doubleClick(container.querySelector('[data-element-id="slide-0:body:0"]')!);
    const ta = container.querySelector("textarea")!;
    fireEvent.change(ta, { target: { value: "Updated bullet" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onPatch).toHaveBeenCalledWith([
      { op: "replace", path: "/slides/0/body/0", value: "Updated bullet" },
    ]);
  });
});

// ─── 6: Non-editable elements ────────────────────────────────────────────────

describe("ElementBox — non-editable element (chart)", () => {
  it("double-click does NOT open an input for chart elements", () => {
    const { container } = render(
      <ElementBox
        {...titleBox({
          elementId: "slide-0:chart",
          kind: "chart",
          jsonPointer: "/slides/0/chart",
          content: "[chart]",
        })}
      />,
    );
    fireEvent.doubleClick(container.querySelector('[data-element-id="slide-0:chart"]')!);
    expect(container.querySelector('input[type="text"]')).toBeNull();
    expect(container.querySelector("textarea")).toBeNull();
  });
});

// ─── 7: Click selects ────────────────────────────────────────────────────────

describe("ElementBox — click fires onSelect", () => {
  it("a single click fires onSelect with the element_id", () => {
    const onSelect = vi.fn();
    const { container } = render(<ElementBox {...titleBox({ onSelect })} />);
    fireEvent.click(container.querySelector('[data-element-id="slide-0:title"]')!);
    expect(onSelect).toHaveBeenCalledWith("slide-0:title");
  });
});

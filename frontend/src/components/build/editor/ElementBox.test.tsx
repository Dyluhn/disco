/**
 * ElementBox font sizing (#5). The element's geometry (x/y/w/h) is a % of the CANVAS,
 * but the font used `vw` (a % of the VIEWPORT). In the editor the canvas is a panel, so
 * `vw` text rendered at the wrong scale and drifted on window resize. The font is now
 * resolved against canvasWidth (px), consistent with the geometry and the rendered deck.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ElementBox } from "./ElementBox";
import type { LoweredElement } from "./types";

function el(overrides: Partial<LoweredElement> = {}): LoweredElement {
  return {
    element_id: "e1",
    slide_id: "s1",
    kind: "chart", // non-editable → renders the box with boxStyle directly
    content: "Q3 revenue",
    geometry: { x: 10, y: 10, w: 80, h: 20 },
    font_size_vw: 2.5,
    font_weight: 400,
    font_style: "normal",
    json_pointer: "/slides/0/elements/0",
    ...overrides,
  } as LoweredElement;
}

describe("ElementBox font sizing (#5)", () => {
  it("resolves font_size_vw against the canvas width (px), never viewport vw", () => {
    const { container } = render(
      <ElementBox
        element={el()}
        selected={false}
        canvasWidth={1000}
        canvasHeight={563}
        onSelect={() => {}}
        onPatch={() => {}}
        disableDrag
      />,
    );
    const box = container.querySelector('[data-element-id="e1"]') as HTMLElement;
    // font_size_vw 2.5 at canvasWidth 1000 → 25px (2.5% of the canvas), NOT "2.5vw".
    expect(box.style.fontSize).toBe("25px");
    expect(box.style.fontSize).not.toContain("vw");
  });

  it("scales with the canvas width (a wider canvas → proportionally larger text)", () => {
    const { container } = render(
      <ElementBox
        element={el({ font_size_vw: 4 })}
        selected={false}
        canvasWidth={1280}
        canvasHeight={720}
        onSelect={() => {}}
        onPatch={() => {}}
        disableDrag
      />,
    );
    const box = container.querySelector('[data-element-id="e1"]') as HTMLElement;
    expect(box.style.fontSize).toBe("51.2px"); // 4% of 1280
  });
});

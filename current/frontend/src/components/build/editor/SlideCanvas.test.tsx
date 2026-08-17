/**
 * SlideCanvas — the substrate must NEVER show a silent black box. When there's no
 * render yet (still fetching, a slow 2.5 MB load, or a transient failure) it shows an
 * explicit "Loading slide preview…" state; once the render HTML arrives the iframe is
 * shown instead. (The black-canvas symptom I hit during the #5d proof.)
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SlideCanvas } from "./SlideCanvas";
import type { ElementMapEntry } from "./SlideCanvas";

const emptyMap = new Map<string, ElementMapEntry>();

function setup(renderHtml: string | null) {
  return render(
    <SlideCanvas
      renderHtml={renderHtml}
      activeSlideId="slide-0"
      elementMap={emptyMap}
      selectedElementId={null}
      onSelectElement={vi.fn()}
      onPatch={vi.fn()}
    />,
  );
}

describe("SlideCanvas no-render state", () => {
  it("shows an explicit loading state (not a silent black box) when renderHtml is null", () => {
    setup(null);
    expect(screen.getByText(/loading slide preview/i)).toBeInTheDocument();
  });

  it("shows the loading state for an empty render string too", () => {
    setup("");
    expect(screen.getByText(/loading slide preview/i)).toBeInTheDocument();
  });

  it("renders the iframe (no loading state) once renderHtml is present", () => {
    const { container } = setup(
      '<html><body><section class="slide active" data-slide-id="slide-0">' +
        '<h1 data-element-id="slide-0:title" data-slide-id="slide-0">Hi</h1>' +
        "</section></body></html>",
    );
    expect(screen.queryByText(/loading slide preview/i)).not.toBeInTheDocument();
    expect(container.querySelector("iframe")).not.toBeNull();
  });
});

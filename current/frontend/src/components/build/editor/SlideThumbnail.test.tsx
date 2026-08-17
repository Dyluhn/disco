/**
 * SlideThumbnail — the rail thumbnail must be a true mini-render of the slide
 * (the SAME renderHtml the main canvas uses), NOT a raw bg_color swatch. This is
 * the fix for the black/blank-thumbnail bug: the Disco theme renders LIGHT slides,
 * but the rail painted the raw dark bg_color → black squares.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SlideThumbnail } from "./SlideThumbnail";
import { activateSlide } from "./slideToggle";

const FIXTURE_HTML = `<!DOCTYPE html><html><head>
<style>.slide{display:none}.slide.active{display:flex}</style></head>
<body><div class="deck">
<section class="slide active" data-slide-id="slide-0"><h2>One</h2></section>
<section class="slide" data-slide-id="slide-1"><h2>Two</h2></section>
</div></body></html>`;

describe("SlideThumbnail", () => {
  it("renders an iframe whose srcDoc is the render HTML (not a bare bg_color div)", () => {
    const { container } = render(
      <SlideThumbnail renderHtml={FIXTURE_HTML} slideId="slide-0" />,
    );
    const iframe = container.querySelector("iframe");
    expect(iframe).not.toBeNull();
    expect(iframe?.getAttribute("srcdoc")).toBe(FIXTURE_HTML);
  });

  it("mirrors SlideCanvas security posture: allow-same-origin, NO allow-scripts", () => {
    const { container } = render(
      <SlideThumbnail renderHtml={FIXTURE_HTML} slideId="slide-0" />,
    );
    const sandbox = container.querySelector("iframe")?.getAttribute("sandbox") ?? "";
    expect(sandbox).toContain("allow-same-origin");
    expect(sandbox).not.toContain("allow-scripts");
  });

  it("is display-only (pointer-events:none + tabIndex -1) so the parent button owns clicks", () => {
    const { container } = render(
      <SlideThumbnail renderHtml={FIXTURE_HTML} slideId="slide-0" />,
    );
    const iframe = container.querySelector("iframe") as HTMLIFrameElement;
    expect(iframe.style.pointerEvents).toBe("none");
    expect(iframe.tabIndex).toBe(-1);
  });

  it("shows a neutral LIGHT placeholder (never a black swatch) before render HTML loads", () => {
    const { container } = render(<SlideThumbnail renderHtml={null} slideId="slide-0" />);
    // No iframe yet, and the placeholder background is light, not a raw dark bg_color.
    expect(container.querySelector("iframe")).toBeNull();
    const placeholder = container.firstElementChild as HTMLElement;
    expect(placeholder.style.background).toBe("rgb(255, 255, 255)");
  });

  // The shared toggle (so the thumbnail can't drift from SlideCanvas): activating a
  // slide id adds .active to ONLY that section and removes it from the others.
  it("activateSlide targets exactly the requested slide section", () => {
    const doc = new DOMParser().parseFromString(FIXTURE_HTML, "text/html");
    const activated = activateSlide(doc, "slide-1");
    expect(activated?.getAttribute("data-slide-id")).toBe("slide-1");
    expect(doc.querySelector('[data-slide-id="slide-1"]')?.classList.contains("active")).toBe(
      true,
    );
    expect(doc.querySelector('[data-slide-id="slide-0"]')?.classList.contains("active")).toBe(
      false,
    );
  });

  it("activateSlide returns null when the doc isn't ready", () => {
    expect(activateSlide(null, "slide-0")).toBeNull();
  });
});

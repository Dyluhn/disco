import "@testing-library/jest-dom/vitest";

// jsdom lacks matchMedia (theme detection) and these layout APIs the UI touches.
if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList;
}

if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// Radix menus call the Pointer Capture APIs jsdom doesn't implement.
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
}

// jsdom lacks getContext; mock it so Chart.js doesn't crash during init
Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
  value: () => ({
    fillRect: () => {},
    clearRect: () => {},
    getImageData: (x: number, y: number, w: number, h: number) => ({
      data: new Uint8ClampedArray(w * h * 4),
    }),
    putImageData: () => {},
    createImageData: () => ({ data: new Uint8ClampedArray(0) }),
    setTransform: () => {},
    drawWidget: () => {},
    measureText: () => ({ width: 0 }),
    canvas: { width: 0, height: 0 },
  }),
});

// Mock Chart.js globally so top-level Chart.register in blocks.tsx doesn't fail
vi.mock("chart.js", () => {
  const ChartMock = vi.fn().mockImplementation(() => ({
    destroy: vi.fn(),
  }));
  (ChartMock as any).register = vi.fn();
  return {
    Chart: ChartMock,
    registerables: [],
  };
});

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SuggestionChips } from "@/components/SuggestionChips";

function buttonTexts(): string[] {
  return screen.getAllByRole("button").map((button) => button.textContent ?? "");
}

function response(suggestions: string[]): Response {
  return {
    ok: true,
    status: 200,
    json: async () => ({ surface: "build", suggestions, source: "generated" }),
  } as Response;
}

describe("SuggestionChips", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders curated chips immediately, then swaps to generated suggestions", async () => {
    const generated = Array.from(
      { length: 8 },
      (_, index) => `generated build prompt ${index + 1}`,
    );
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response(generated));

    render(<SuggestionChips surface="build" onPick={vi.fn()} />);

    expect(buttonTexts()).toHaveLength(4);
    expect(buttonTexts().some((text) => text.startsWith("generated"))).toBe(false);

    await waitFor(() => {
      expect(buttonTexts().every((text) => generated.includes(text))).toBe(true);
    });
  });

  it("keeps curated chips when generated suggestions fail", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("offline"));

    render(<SuggestionChips surface="agent" onPick={vi.fn()} />);
    const initial = buttonTexts();

    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(1));
    expect(buttonTexts()).toEqual(initial);
  });
});

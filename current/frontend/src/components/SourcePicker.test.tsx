import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourcePicker } from "./SourcePicker";

// The configured provider stays in Settings. This control exposes only
// additive News/arXiv/Semantic Scholar sources for the current run.

function Harness({ initial = [] as string[] }: { initial?: string[] }) {
  const [selected, setSelected] = useState<string[]>(initial);
  return (
    <>
      <SourcePicker selected={selected} onChange={setSelected} />
      <output data-testid="selected-sources">{selected.join(",")}</output>
    </>
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SourcePicker", () => {
  it("shows only the keyless federation sources — no provider chips, no 'Add in Settings'", () => {
    render(<Harness />);

    expect(screen.getByText("Additional sources")).toBeVisible();
    // Keyless sources present + enabled.
    expect(screen.getByRole("button", { name: /news/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /arxiv/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /semantic scholar/i })).toBeEnabled();

    // The provider chips Dylan asked to remove are GONE.
    expect(screen.queryByRole("button", { name: /tavily/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /brave/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /searxng/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^web$/i })).toBeNull();

    // The bush-league "Add in Settings ->" link is gone (no links at all).
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.queryByText(/add in settings/i)).toBeNull();
  });

  it("defaults to an empty selection so search runs the configured provider", () => {
    render(<Harness />);
    expect(screen.getByTestId("selected-sources")).toHaveTextContent("");
    expect(screen.getByRole("button", { name: /news/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("toggles sources on and off", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("button", { name: /arxiv/i }));
    expect(screen.getByTestId("selected-sources")).toHaveTextContent("arxiv");

    await user.click(screen.getByRole("button", { name: /news/i }));
    expect(screen.getByTestId("selected-sources")).toHaveTextContent("arxiv,news");

    // toggling off removes it
    await user.click(screen.getByRole("button", { name: /arxiv/i }));
    expect(screen.getByTestId("selected-sources")).toHaveTextContent("news");
  });
});

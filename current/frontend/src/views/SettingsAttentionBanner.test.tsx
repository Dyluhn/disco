import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useSecrets", () => ({
  useSecrets: () => ({ data: { locked_names: ["TAVILY_API_KEY"] } }),
}));
vi.mock("@/hooks/useModels", () => ({
  useAssignments: () => ({ data: undefined }),
  useImageGenConfig: () => ({ data: undefined }),
  useModels: () => ({ data: undefined }),
  useOpenRouterKey: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useProjectsConfig", () => ({
  useProjectsConfig: () => ({ data: undefined }),
}));

import { SettingsAttentionBanner } from "./SettingsView";

describe("SettingsAttentionBanner provider-key target", () => {
  it("opens the advanced provider-key disclosure, not the first provider disclosure", async () => {
    const user = userEvent.setup();
    render(
      <>
        <SettingsAttentionBanner />
        <section id="provider-connections">
          <details data-testid="openrouter-details">
            <summary>OpenRouter</summary>
          </details>
          <details id="advanced-provider-keys">
            <summary>Advanced provider keys</summary>
          </details>
        </section>
      </>,
    );

    const review = screen.getByRole("link", { name: "Review providers" });
    expect(review).toHaveAttribute("href", "#advanced-provider-keys");
    await user.click(review);

    expect(screen.getByTestId("openrouter-details")).not.toHaveAttribute("open");
    expect(document.querySelector("#advanced-provider-keys")).toHaveAttribute("open");
  });
});

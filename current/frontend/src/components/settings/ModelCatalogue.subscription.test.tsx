/**
 * W-05: a SUBSCRIPTION model must not be edited through the per-token price fields
 * (showing "0 = free" reads as free — the exact bug). When Pricing is set to
 * Subscription the price inputs are hidden and replaced by a "Subscription" note;
 * Metered keeps the price fields. Drives the real ModelCatalogue form; only the
 * data-hook boundary is stubbed.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ModelCatalogue } from "./ModelCatalogue";

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: [] }),
  useCreateModel: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
  useUpdateModel: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
  useDeleteModel: () => ({ mutate: vi.fn(), error: null }),
}));

vi.mock("@/hooks/useSecrets", () => ({
  useSecrets: () => ({ data: { names: [] } }),
  useSetSecret: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
}));

describe("ModelCatalogue subscription pricing", () => {
  it("hides the per-token price fields and shows 'Subscription' when Pricing is subscription", () => {
    render(<ModelCatalogue />);
    // Open the Add-model dialog (header button is the only "Add model" before open).
    fireEvent.click(screen.getByText("Add model"));

    // The everyday fields stay visible; technical metadata is opt-in.
    expect(screen.getByText("Catalogue id")).toBeDefined();
    const advanced = screen.getByText("Advanced model metadata");
    expect(advanced.closest("details")).not.toHaveAttribute("open");
    fireEvent.click(advanced);
    expect(advanced.closest("details")).toHaveAttribute("open");

    // Default Metered → the per-token price fields are present.
    expect(screen.getByText("Price in / Mtok (0 = free)")).toBeDefined();
    expect(screen.getByText("Price out / Mtok")).toBeDefined();
    expect(screen.getByText("Maximum output tokens (optional)")).toBeDefined();

    // Switch Pricing → Subscription.
    fireEvent.change(screen.getByRole("combobox", { name: "Pricing" }), {
      target: { value: "subscription" },
    });

    // The price fields are gone (no "0 = free"), and the Subscription note is shown.
    expect(screen.queryByText("Price in / Mtok (0 = free)")).toBeNull();
    expect(screen.queryByText("Price out / Mtok")).toBeNull();
    expect(screen.getByText(/Subscription — a flat-rate plan/)).toBeDefined();
  });
});

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { StoredCredentialField } from "./StoredCredentialField";

vi.mock("@/hooks/useSecrets", () => ({
  useSecrets: () => ({
    data: {
      names: ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
      locked_names: [],
      locked: false,
      can_store: true,
    },
  }),
  useSetSecret: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));

describe("StoredCredentialField", () => {
  it("suggests stored names while preserving custom environment names", () => {
    const onChange = vi.fn();
    render(
      <StoredCredentialField
        label="Model credential"
        value="LEGACY_CUSTOM_KEY"
        onChange={onChange}
        placeholder="MODEL_API_KEY"
      />,
    );

    const input = screen.getByLabelText("Model credential");
    expect(input).toHaveValue("LEGACY_CUSTOM_KEY");
    const listId = input.getAttribute("list");
    expect(listId).toBeTruthy();
    expect(
      document.querySelector(`#${CSS.escape(listId!)} option[value="OPENAI_API_KEY"]`),
    ).toBeInTheDocument();
    expect(
      document.querySelector(`#${CSS.escape(listId!)} option[value="LEGACY_CUSTOM_KEY"]`),
    ).toBeInTheDocument();
    expect(screen.queryByText(/sk-/i)).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: "  Mixed_Case_KEY  " } });
    expect(onChange).toHaveBeenCalledWith("  Mixed_Case_KEY  ");
  });

  it("lets a user choose a stored key or add one without exposing stored values", async () => {
    const onChange = vi.fn();
    render(
      <StoredCredentialField
        label="Search credential"
        value=""
        onChange={onChange}
        placeholder="SEARCH_API_KEY"
        suggestedName="TAVILY_API_KEY"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /choose or add/i }));
    const dialog = screen.getByRole("dialog", { name: /choose or add credential/i });
    fireEvent.click(within(dialog).getByRole("button", { name: /OPENAI_API_KEY/i }));
    expect(onChange).toHaveBeenCalledWith("OPENAI_API_KEY");

    fireEvent.click(screen.getByRole("button", { name: /choose or add/i }));
    const addDialog = screen.getByRole("dialog", { name: /choose or add credential/i });
    expect(within(addDialog).getByLabelText("New credential name")).toHaveValue("TAVILY_API_KEY");
    fireEvent.change(within(addDialog).getByLabelText("New credential value"), {
      target: { value: "tvly-secret" },
    });
    fireEvent.click(within(addDialog).getByRole("button", { name: /save credential/i }));
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith("TAVILY_API_KEY"));
    expect(screen.queryByText("tvly-secret")).not.toBeInTheDocument();
  });
});

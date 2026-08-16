import { fireEvent, render, screen } from "@testing-library/react";
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
});

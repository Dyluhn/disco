import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ModelCatalogue } from "./ModelCatalogue";

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: [] }),
  useCreateModel: () => ({
    mutateAsync: vi.fn(), isPending: false,
    error: new Error(JSON.stringify({ detail: [{
      loc: ["body", "request_policy", "body"],
      msg: "Request policy cannot override owned fields: ['messages']",
      input: { messages: ["private submitted data"] },
    }] })),
  }),
  useUpdateModel: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
  useDeleteModel: () => ({ mutate: vi.fn(), error: null }),
}));
vi.mock("@/hooks/useSecrets", () => ({ useSecrets: () => ({ data: { names: [] } }) }));

it("shows the validation explanation without raw response or submitted values", () => {
  render(<ModelCatalogue />);
  fireEvent.click(screen.getByText("Add model"));
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Request policy cannot override owned fields: ['messages']",
  );
  expect(screen.getByRole("alert")).not.toHaveTextContent("private submitted data");
  expect(screen.getByRole("alert")).not.toHaveTextContent('"detail"');
  expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
});

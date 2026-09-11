import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useReferencePacks", () => ({
  useReferencePacks: () => ({
    isLoading: false,
    data: [
      {
        pack_id: "rp_1", name: "Brand kit", description: "Logos", file_count: 3,
        total_bytes: 2048, created_at: "2026-09-11T00:00:00Z", updated_at: "2026-09-11T00:00:00Z", digest: "d",
      },
      {
        pack_id: "rp_2", name: "Spec", description: "", file_count: 1,
        total_bytes: 100, created_at: "2026-09-11T00:00:00Z", updated_at: "2026-09-11T00:00:00Z", digest: "e",
      },
    ],
  }),
}));

import { ReferencePackPicker } from "./ReferencePackPicker";

describe("ReferencePackPicker", () => {
  it("shows the selection count and toggles packs", () => {
    const onChange = vi.fn();
    render(<ReferencePackPicker selected={["rp_1"]} onChange={onChange} defaultOpen />);
    const trigger = screen.getByRole("button", { name: "Reference packs", hidden: true });
    expect(trigger).toHaveTextContent("References (1)");
    fireEvent.click(screen.getByText("Spec"));
    expect(onChange).toHaveBeenCalledWith(["rp_1", "rp_2"]);
    fireEvent.click(screen.getByText("Brand kit"));
    expect(onChange).toHaveBeenCalledWith([]);
  });
});

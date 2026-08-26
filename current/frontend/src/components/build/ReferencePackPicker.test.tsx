import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ReferencePackPicker } from "@/components/buildSurface/ReferencePackPicker";

const packs = [
  {
    id: "pack-1",
    name: "Brand",
    description: "Brand references",
    revision: "rev-1",
    files: [{ id: "file-1", name: "brand.md", media_type: "text/markdown", size_bytes: 4 }],
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

vi.mock("@/hooks/useReferencePacks", () => ({
  useReferencePacks: () => ({ data: packs, isLoading: false }),
}));

vi.mock("@/api/referencePacks", async () => {
  const actual = await vi.importActual<typeof import("@/api/referencePacks")>("@/api/referencePacks");
  return { ...actual, referencePacksAvailable: () => true };
});

describe("ReferencePackPicker", () => {
  it("adds an unselected pack", () => {
    const onChange = vi.fn();
    const view = render(<ReferencePackPicker selected={[]} onChange={onChange} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "Use Brand" }));
    expect(onChange).toHaveBeenCalledWith(packs);
    view.rerender(<ReferencePackPicker selected={packs} onChange={onChange} />);
    expect(screen.getByRole("checkbox", { name: "Use Brand" })).toHaveAttribute("aria-checked", "true");
  });

  it("removes a selected pack", () => {
    const onChange = vi.fn();
    render(<ReferencePackPicker selected={packs} onChange={onChange} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "Use Brand" }));
    expect(onChange).toHaveBeenCalledWith([]);
  });
});

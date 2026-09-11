import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const deleteMutate = vi.fn(() => Promise.resolve());
vi.mock("@/hooks/useReferencePacks", () => ({
  useReferencePacks: () => ({
    isLoading: false,
    error: null,
    data: [
      {
        pack_id: "rp_1", name: "Brand kit", description: "Logos and copy", file_count: 2,
        total_bytes: 4096, created_at: "2026-09-11T00:00:00Z", updated_at: "2026-09-11T00:00:00Z", digest: "d",
      },
    ],
  }),
  useReferencePack: () => ({
    data: {
      pack_id: "rp_1", name: "Brand kit", description: "Logos and copy", file_count: 2,
      total_bytes: 4096, created_at: "", updated_at: "", digest: "d",
      files: [
        { name: "copy.md", media_type: "text/markdown", bytes: 96, sha256: "a", state: "ready" },
        { name: "logo.png", media_type: "image/png", bytes: 4000, sha256: "b", state: "asset_only" },
      ],
    },
  }),
  useUpdateReferencePack: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useAddReferencePackFiles: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useRemoveReferencePackFile: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useDeleteReferencePack: () => ({ isPending: false, mutateAsync: deleteMutate }),
}));

import { ReferencePacksSection } from "./ReferencePacksSection";

describe("ReferencePacksSection", () => {
  it("lists packs, shows file states when expanded, and deletes only after confirmation", () => {
    render(<ReferencePacksSection />);
    expect(screen.getByText("Brand kit")).toBeInTheDocument();
    expect(screen.getByText(/2 files/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText("copy.md")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Asset only")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete Brand kit" }));
    expect(screen.getByText(/Future Builds can no longer select it/)).toBeInTheDocument();
    expect(deleteMutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Delete pack" }));
    expect(deleteMutate).toHaveBeenCalledWith(["rp_1"]);
  });
});

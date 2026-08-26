import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ReferencePackPicker } from "@/components/build/ReferencePackPicker";
import type { ReferencePack } from "@/api/referencePacks";

const packs: ReferencePack[] = [
  {
    id: "pack-1",
    name: "AppKit",
    description: "Trusted components",
    current_version_id: "version-1",
    current: { id: "version-1", description: "", content_sha256: "sha-1", files: [], total_bytes: 4, created_at: "" },
    files: [{ name: "components.md", path: "components.md", media_type: "text/markdown", size: 4, sha256: "x" }],
    created_at: "",
    updated_at: "",
  },
];

vi.mock("@/api/client", () => ({ agentLive: () => true }));
vi.mock("@/hooks/useReferencePacks", () => ({
  useReferencePacks: () => ({ data: packs, isLoading: false }),
}));

describe("ReferencePackPicker", () => {
  it("lets a Build select a pack and exposes the immutable snapshot affordance", () => {
    const onChange = vi.fn();
    const view = render(<ReferencePackPicker selected={[]} onChange={onChange} />);
    const pack = screen.getByRole("checkbox", { name: /use appkit/i });
    fireEvent.click(pack);
    expect(onChange).toHaveBeenCalledWith(packs);
    view.rerender(<ReferencePackPicker selected={packs} onChange={onChange} />);
    expect(screen.getByText(/snapshotted when this build starts/i)).toBeInTheDocument();
  });

  it("removes an already-selected pack without mutating the source list", () => {
    const onChange = vi.fn();
    render(<ReferencePackPicker selected={packs} onChange={onChange} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /use appkit/i }));
    expect(onChange).toHaveBeenCalledWith([]);
    expect(packs).toHaveLength(1);
  });
});

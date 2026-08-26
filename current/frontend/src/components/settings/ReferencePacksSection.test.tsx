import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ReferencePacksSection } from "@/components/settings/ReferencePacksSection";

const updateMutate = vi.fn();
const filesMutate = vi.fn();
const pack = {
  id: "pack-1",
  name: "AppKit",
  description: "Trusted components",
  current_version_id: "version-1",
  current: {
    id: "version-1",
    description: "Trusted components",
    content_sha256: "sha-1",
    files: [],
    total_bytes: 5,
    created_at: "",
  },
  files: [
    { name: "rbac.md", path: "rbac.md", media_type: "text/markdown", size: 5, sha256: "file-1" },
  ],
  created_at: "",
  updated_at: "",
};

vi.mock("@/hooks/useReferencePacks", () => ({
  useReferencePacks: () => ({ data: [pack], isLoading: false }),
  useUpdateReferencePack: () => ({ mutate: updateMutate, isPending: false, error: null }),
  useUpdateReferencePackFiles: () => ({ mutate: filesMutate, isPending: false, error: null }),
  useDeleteReferencePack: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));

describe("ReferencePacksSection", () => {
  it("keeps creation and raw workspace/conversation fields out of Settings", () => {
    render(<ReferencePacksSection />);
    expect(screen.queryByRole("button", { name: /new pack/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/conversation id/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/file paths/i)).not.toBeInTheDocument();
    expect(screen.getByText("rbac.md")).toBeInTheDocument();
  });

  it("updates metadata only, preserving the immutable file snapshot", () => {
    render(<ReferencePacksSection />);
    fireEvent.click(screen.getByRole("button", { name: /edit appkit/i }));
    const editor = screen.getByRole("textbox", { name: /reference pack name/i }).closest("form");
    expect(editor).not.toBeNull();
    const form = within(editor!);
    fireEvent.change(form.getByRole("textbox", { name: /reference pack name/i }), { target: { value: "AppKit UI" } });
    fireEvent.change(form.getByRole("textbox", { name: /reference pack description/i }), { target: { value: "Updated" } });
    fireEvent.click(form.getByRole("button", { name: /save changes/i }));
    expect(updateMutate).toHaveBeenCalledWith(
      { id: "pack-1", patch: { name: "AppKit UI", description: "Updated" } },
      expect.any(Object),
    );
    expect(updateMutate.mock.calls.at(-1)?.[0]).not.toHaveProperty("patch.files");
    expect(updateMutate.mock.calls.at(-1)?.[0]).not.toHaveProperty("patch.conversation_id");
  });

  it("batches exact file removals and local replacements", () => {
    render(<ReferencePacksSection />);
    fireEvent.click(screen.getByRole("button", { name: "Remove rbac.md" }));
    const input = screen.getByLabelText("Add or replace files for AppKit");
    const replacement = new File(["new"], "rbac.md", { type: "text/markdown" });
    fireEvent.change(input, { target: { files: [replacement] } });
    fireEvent.click(screen.getByRole("button", { name: "Save files" }));
    expect(filesMutate).toHaveBeenCalledWith(
      { id: "pack-1", files: [replacement], remove: [] },
      expect.any(Object),
    );
  });
});

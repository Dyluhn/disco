import { afterEach, describe, expect, it, vi } from "vitest";
import {
  bindReferencePacks,
  bindReferencePacksBeforeSubmit,
  updateReferencePack,
  updateReferencePackFiles,
  type ReferencePack,
} from "@/api/referencePacks";
import { agentMultipart, agentSend } from "@/api/client";

vi.mock("@/api/client", () => ({
  agentLive: () => true,
  agentGet: vi.fn(),
  agentMultipart: vi.fn(),
  agentSend: vi.fn(),
  fixtureDelay: vi.fn(),
}));

describe("Reference Pack API", () => {
  afterEach(() => vi.clearAllMocks());

  it("binds exact current pack versions before a first Build turn", async () => {
    vi.mocked(agentSend).mockResolvedValue({ binding_id: "binding-1" });
    await bindReferencePacks("conv-1", [
      { pack_id: "pack-1", version_id: "version-7", content_sha256: "sha-7" },
    ]);
    expect(agentSend).toHaveBeenCalledWith(
      "POST",
      "/api/conversations/conv-1/reference-packs/bind",
      {
        selections: [
          { pack_id: "pack-1", version_id: "version-7", content_sha256: "sha-7" },
        ],
      },
    );
  });

  it("updates a pack through the single owner-scoped route", async () => {
    vi.mocked(agentSend).mockResolvedValue({ id: "pack-1" });
    await updateReferencePack("pack-1", { name: "Updated" });
    expect(agentSend).toHaveBeenCalledWith(
      "PATCH",
      "/api/reference-packs/pack-1",
      { name: "Updated" },
    );
  });

  it("sends browser files and exact removals as one multipart batch", async () => {
    vi.mocked(agentMultipart).mockResolvedValue({ id: "pack-1" });
    const file = new File(["hello"], "docs/readme.md", { type: "text/markdown" });
    await updateReferencePackFiles("pack-1", [file], ["old.md"]);
    expect(agentMultipart).toHaveBeenCalledWith(
      "PUT",
      "/api/reference-packs/pack-1/files",
      expect.any(FormData),
    );
    const form = vi.mocked(agentMultipart).mock.calls.at(-1)?.[2] as FormData;
    expect(form.get("remove")).toBe("old.md");
    expect((form.get("files") as File).name).toBe("docs/readme.md");
  });

  it("completes binding before submit and never submits after a bind failure", async () => {
    const order: string[] = [];
    const selected = [{
      id: "pack-1",
      current_version_id: "version-7",
      current: { content_sha256: "sha-7" },
    }] as ReferencePack[];
    vi.mocked(agentSend).mockImplementation(async () => {
      order.push("bind");
      return { binding_id: "binding-1" };
    });
    await bindReferencePacksBeforeSubmit(
      selected,
      async () => {
        order.push("conversation");
        return "conv-1";
      },
      () => order.push("submit"),
    );
    expect(order).toEqual(["conversation", "bind", "submit"]);

    order.length = 0;
    vi.mocked(agentSend).mockRejectedValue(new Error("bind failed"));
    await expect(
      bindReferencePacksBeforeSubmit(selected, async () => "conv-1", () => order.push("submit")),
    ).rejects.toThrow("bind failed");
    expect(order).toEqual([]);
  });
});

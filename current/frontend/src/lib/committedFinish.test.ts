import { describe, expect, it } from "vitest";
import type { AgentEvent } from "@/types/agent";
import { selectCommittedFinish } from "./committedFinish";

const status = (seq: number, value: "FINISHED" | "RUNNING" = "FINISHED"): AgentEvent => ({
  id: `status-${seq}`,
  seq,
  kind: "status",
  status: value,
});

const version = (
  seq: number,
  terminalSeq: number,
  versionSeq = 3,
  treeDigest = "tree-3",
): AgentEvent => ({
  id: `version-${seq}`,
  seq,
  kind: "workspace_version",
  trigger: "finish",
  version_seq: versionSeq,
  tree_digest: treeDigest,
  final_seal: {
    schema_version: 1,
    scope: { namespace: "workspace.tree", identifier: "cid" },
    terminal_seq: terminalSeq,
    latest_effect_seq: null,
    version_seq: versionSeq,
    tree_digest: treeDigest,
    file_count: 1,
    total_bytes: 1,
  },
});

describe("selectCommittedFinish", () => {
  it("waits for the matching seal, regardless of replay ordering", () => {
    expect(selectCommittedFinish([status(10)], "FINISHED")).toBeNull();
    expect(selectCommittedFinish([version(12, 10), status(10)], "FINISHED")?.versionSeq).toBe(3);
  });

  it("rejects stale and mismatched seals", () => {
    expect(selectCommittedFinish([status(20), version(12, 10)], "FINISHED")).toBeNull();
    expect(selectCommittedFinish([status(20), version(12, 20, 4, "old")], "FINISHED")?.versionSeq).toBe(4);
    expect(selectCommittedFinish([status(20), { ...version(12, 20), tree_digest: "tampered" }], "FINISHED")).toBeNull();
  });

  it("selects the newest finished iteration and never exposes it while running", () => {
    const events = [status(10), version(12, 10), status(20), version(22, 20, 4, "tree-4")];
    expect(selectCommittedFinish(events, "FINISHED")?.terminalSeq).toBe(20);
    expect(selectCommittedFinish(events, "RUNNING")).toBeNull();
  });
});

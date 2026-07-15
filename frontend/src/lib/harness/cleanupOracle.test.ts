import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { describe, expect, it, vi } from "vitest";

import {
  attemptAllCleanup,
  createRegisteredTarget,
  rethrowAfterBestEffortReport,
} from "./cleanupOracle";
import {
  PROVIDER_CONVERSATION_MANIFEST_ENV,
  updateProviderConversationManifest,
} from "./providerManifest";

describe("attemptAllCleanup", () => {
  it("attempts every target in teardown order and reports aggregate failures", async () => {
    const attempted: string[] = [];
    const remove = vi.fn(async (target: string) => {
      attempted.push(target);
      if (target === "second") throw new Error("sensitive failure body");
    });

    await expect(
      attemptAllCleanup(["first", "second", "third"], remove),
    ).resolves.toEqual({
      attempted: 3,
      failed: 1,
    });
    expect(attempted).toEqual(["third", "second", "first"]);
  });

  it("reports an all-success cleanup without retaining identifiers", async () => {
    await expect(
      attemptAllCleanup([1, 2], async () => undefined),
    ).resolves.toEqual({
      attempted: 2,
      failed: 0,
    });
  });

  it("retains manifest and cleanup ownership when the post-create kick fails", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "disco-registration-"));
    const manifest = path.join(root, "provider-conversations.json");
    const owned: string[] = [];
    const env = {
      DISCO_RELIABILITY_SUITE_OUT: root,
      [PROVIDER_CONVERSATION_MANIFEST_ENV]: manifest,
    };
    const kickFailure = new Error("kick failed");
    try {
      await expect(
        createRegisteredTarget(
          async () => "build-id",
          (id) => {
            owned.push(id);
            updateProviderConversationManifest(owned, env);
          },
          async () => {
            throw kickFailure;
          },
        ),
      ).rejects.toBe(kickFailure);
      expect(JSON.parse(fs.readFileSync(manifest, "utf-8")).conversation_ids).toEqual([
        "build-id",
      ]);
      const remove = vi.fn(async () => undefined);
      await expect(attemptAllCleanup(owned, remove)).resolves.toEqual({
        attempted: 1,
        failed: 0,
      });
      expect(remove).toHaveBeenCalledWith("build-id");
    } finally {
      fs.rmSync(root, { force: true, recursive: true });
    }
  });

  it("rethrows the identical primary failure when supplemental reporting fails", async () => {
    const primary = new Error("primary failure");
    const report = vi.fn(async () => {
      throw new Error("attachment failure");
    });

    await expect(rethrowAfterBestEffortReport(primary, report)).rejects.toBe(primary);
    expect(report).toHaveBeenCalledOnce();
  });
});

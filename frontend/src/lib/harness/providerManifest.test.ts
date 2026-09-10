import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  PROVIDER_CONVERSATION_MANIFEST_ENV,
  updateProviderConversationManifest,
} from "./providerManifest";

const roots: string[] = [];

afterEach(() => {
  for (const root of roots.splice(0))
    fs.rmSync(root, { force: true, recursive: true });
});

function fixture() {
  const root = fs.mkdtempSync(
    path.join(os.tmpdir(), "disco-provider-manifest-"),
  );
  roots.push(root);
  const manifest = path.join(root, "provider-conversations.json");
  const env = {
    DISCO_RELIABILITY_SUITE_OUT: root,
    [PROVIDER_CONVERSATION_MANIFEST_ENV]: manifest,
  };
  return { root, manifest, env };
}

describe("updateProviderConversationManifest", () => {
  it("durably records Build scope before later assertions and atomically appends Agent", () => {
    const { manifest, env } = fixture();
    updateProviderConversationManifest(["build-id"], env);
    expect(
      JSON.parse(fs.readFileSync(manifest, "utf-8")).conversation_ids,
    ).toEqual(["build-id"]);

    updateProviderConversationManifest(["build-id", "agent-id"], env);
    expect(
      JSON.parse(fs.readFileSync(manifest, "utf-8")).conversation_ids,
    ).toEqual(["build-id", "agent-id"]);
    expect(fs.statSync(manifest).mode & 0o777).toBe(0o600);
    expect(
      fs
        .readdirSync(path.dirname(manifest))
        .filter((name) => name.endsWith(".tmp")),
    ).toEqual([]);
  });

  it.each([
    ["duplicate", ["build-id", "build-id"]],
    ["empty", []],
    ["too many", ["build-id", "agent-id", "extra-id"]],
  ])("rejects %s scope", (_label, ids) => {
    const { env } = fixture();
    expect(() => updateProviderConversationManifest(ids, env)).toThrow();
  });

  it("rejects replacement, removal, and reordering of durable scope", () => {
    const { env } = fixture();
    updateProviderConversationManifest(["build-id", "agent-id"], env);
    for (const ids of [
      ["build-id"],
      ["agent-id", "build-id"],
      ["other-id", "agent-id"],
    ])
      expect(() => updateProviderConversationManifest(ids, env)).toThrow(
        "must preserve the existing ID prefix",
      );
  });

  it("rejects an existing symlink or extra-key manifest", () => {
    const symlinkFixture = fixture();
    const target = path.join(symlinkFixture.root, "target.json");
    fs.writeFileSync(
      target,
      JSON.stringify({ schema_version: 1, conversation_ids: ["build-id"] }),
    );
    fs.symlinkSync(target, symlinkFixture.manifest);
    expect(() =>
      updateProviderConversationManifest(["build-id"], symlinkFixture.env),
    ).toThrow();

    const extraFixture = fixture();
    fs.writeFileSync(
      extraFixture.manifest,
      JSON.stringify({
        schema_version: 1,
        conversation_ids: ["build-id"],
        unexpected: true,
      }),
    );
    expect(() =>
      updateProviderConversationManifest(["build-id"], extraFixture.env),
    ).toThrow("malformed");
  });

  it("repairs mode on an idempotent exact-prefix update", () => {
    const { manifest, env } = fixture();
    updateProviderConversationManifest(["build-id"], env);
    fs.chmodSync(manifest, 0o644);

    updateProviderConversationManifest(["build-id"], env);

    expect(fs.statSync(manifest).mode & 0o777).toBe(0o600);
  });

  it("rejects a manifest outside the suite output", () => {
    const { root, env } = fixture();
    env[PROVIDER_CONVERSATION_MANIFEST_ENV] = path.join(
      path.dirname(root),
      "outside.json",
    );
    expect(() => updateProviderConversationManifest(["build-id"], env)).toThrow(
      "suite-private",
    );
  });
});

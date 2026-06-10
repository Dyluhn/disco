/**
 * BP-15 — isolation tier map: backend name → IsolationInfo.
 */

import { describe, expect, it } from "vitest";
import { isolationForBackend } from "@/lib/isolation";

describe("isolationForBackend — tier map", () => {
  it("gvisor → adversarialSafe true", () => {
    const info = isolationForBackend("gvisor");
    expect(info).not.toBeNull();
    expect(info!.adversarialSafe).toBe(true);
    expect(info!.label).toMatch(/gvisor/i);
  });

  it("podman → adversarialSafe false", () => {
    const info = isolationForBackend("podman");
    expect(info).not.toBeNull();
    expect(info!.adversarialSafe).toBe(false);
    expect(info!.label).toMatch(/container/i);
  });

  it("local → adversarialSafe false, shared host kernel", () => {
    const info = isolationForBackend("local");
    expect(info).not.toBeNull();
    expect(info!.adversarialSafe).toBe(false);
    expect(info!.label).toMatch(/shared host kernel/i);
  });

  it("process → warning copy (dev mode)", () => {
    const info = isolationForBackend("process");
    expect(info).not.toBeNull();
    expect(info!.adversarialSafe).toBe(false);
    expect(info!.label).toMatch(/no isolation/i);
    expect(info!.tier).toMatch(/⚠/);
  });

  it("null → null (no state yet → renders '…')", () => {
    expect(isolationForBackend(null)).toBeNull();
  });

  it("undefined → null", () => {
    expect(isolationForBackend(undefined)).toBeNull();
  });

  it("unknown backend → null", () => {
    expect(isolationForBackend("unknown-backend")).toBeNull();
  });
});

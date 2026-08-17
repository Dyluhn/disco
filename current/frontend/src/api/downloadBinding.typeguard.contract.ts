/**
 * Regression (NON-FROZEN, criterion-6 guard) — a COMPILE-CHECKED proof that the
 * self-host download binding demands a CONCRETE (non-null) source. It is checked in
 * isolation by its own tsconfig (mirroring the frozen G11 pattern, but this file is a
 * NEW regression, not a frozen contract):
 *
 *     cd frontend && npx tsc -p tsconfig.downloadBinding-guard.json --noEmit   # exit 0
 *
 * The `@ts-expect-error` lines pin the guard: passing a nullable `version_seq` /
 * `spec_digest` (the real wire shape of an unsnapshotted release) where the strict
 * `DownloadBinding` requires a concrete `number` / `string` MUST be a type error. If
 * someone later loosens `DownloadBinding` to accept `number | null` / `string | null`,
 * those errors disappear, the `@ts-expect-error` directives become UNUSED (TS2578),
 * and this file fails to compile — which is exactly the regression we want to catch.
 *
 * This module is a pure type contract: it is a `.contract.ts` (outside vitest's
 * `.test.` glob), never imported by product code, so it is never executed — its only
 * job is to type-check.
 */

import { downloadProject } from "./projects";
import type { ReleaseResponse } from "@/types/release";

// The nullable source fields exactly as production types them (backend `int | None` /
// `str | None`): an unsnapshotted workspace names no concrete committed source.
declare const nullableRelease: Pick<ReleaseResponse, "version_seq" | "spec_digest">;

/** RED-by-design: an unnarrowed nullable field cannot satisfy the strict binding. */
export function bindingRejectsNullableFields(): void {
  // @ts-expect-error version_seq is `number | null`; the strict binding demands a concrete number.
  void downloadProject("conv_x", { version_seq: nullableRelease.version_seq, spec_digest: "sha256:x" });
  // @ts-expect-error spec_digest is `string | null`; the strict binding demands a concrete string.
  void downloadProject("conv_x", { version_seq: 3, spec_digest: nullableRelease.spec_digest });
}

/** GREEN-by-design: a fully-narrowed binding, and an explicit null (unbound), both
 * remain valid — so an over-tightening (e.g. dropping the null form) is also caught. */
export function bindingAcceptsConcreteAndNull(): void {
  void downloadProject("conv_x", { version_seq: 3, spec_digest: "sha256:x" });
  void downloadProject("conv_x", null);
}

/**
 * G08 red (R4-ACTIVATED) — the candidate self-host download must be SOURCE-BOUND,
 * but the client issues an UNBOUND download URL.
 *
 * This is the single frontend-binding red that ALSO consolidates the G11 concern: a
 * null source binding must never ride a bound download. The guard is R4 work at the
 * URL-construction seam — `downloadProject` (`api/projects.ts:187`, wired at
 * `BuildSurface.tsx:606/616`) must carry the binding, and `types/release.ts:73-74`
 * must make `version_seq`/`tree_digest` nullable — NOT a marker stamped on the panel.
 * There is therefore no honest, non-prescriptive R0 vitest red for the null-binding
 * case (the frontend bound-download flow does not exist until R4); this e2e is the
 * behavioral gate for both the bound-binding contract and the null-binding guard.
 *
 * WO-C2 (§6) locks the self-host action to an immutable binding — e.g.
 * `/download?version_seq=N&spec_digest=D`, with BOTH values enforced server-side —
 * and "a bound download never silently falls back to an unbound/plain zip." But
 * `downloadProject(cid)` (`api/projects.ts:182`) requests
 * `/api/projects/{cid}/download` with NO query params: the version/spec binding from
 * the `/release` verdict is dropped, so the byte stream is never pinned to the exact
 * immutable version the verdict describes.
 *
 * This mirrors the `selfhost-candidate.spec.ts` fixture-mode pattern: the candidate
 * verdict is the offline `conv_demo_snake` fixture (`self_host:true`, `version_seq:3`,
 * `spec_digest:"sha256:demo-candidate-0001"`). Triggering the candidate self-host
 * download and observing the `/download` request must show the query carrying that
 * exact `version_seq` + `spec_digest`.
 *
 * REACHABILITY / OFFLINE PRECONDITION (deferred, shared with G09/R4 + the candidate
 * spec): in the current offline app the SelfHostPanel mounts only for a FINISHED build
 * whose cid is a demo release cid; opening `/build/conv_demo_snake` resumes WITHOUT a
 * kick, so it stays IDLE and the panel never mounts. Furthermore, offline
 * `downloadProject` short-circuits before any fetch (`agentLive()===false`), so no
 * `/download` request is emitted at all in fixture mode. This spec is authored to the
 * intended bound-download contract; on baseline it is RED, failing NOW at the
 * reachability precondition (`expect(panel).toBeVisible()`, ~line 53) because the
 * candidate panel never mounts offline. The BINDING TEETH — the `/download?` request
 * match (~lines 58-59) and the `version_seq`/`spec_digest` assertions (~lines 66-67)
 * — do NOT execute until R4 restores panel reachability + wires the bound download;
 * only then, against a live agent, do they turn green. R4-ACTIVATED and deferred,
 * exactly like the candidate/needs-review proofs in this folder.
 */

import { expect, test } from "@playwright/test";

test.describe("WO-C2 §6 — the candidate self-host download is source-bound (frozen Firefox proof)", () => {
  test("the /download request carries the exact version_seq + spec_digest from /release", async ({
    page,
  }) => {
    await page.goto("/build/conv_demo_snake");

    // Reachability precondition (shared with G09/R4/G10): the candidate panel mounts.
    const panel = page.locator('[data-disco-control="build.self-host"]');
    await expect(panel).toBeVisible();

    // WO-C2 §6: the self-host download request must be BOUND — its query carries the
    // immutable (version_seq, spec_digest) named by the /release verdict, never an
    // unbound/plain URL. Capture the request the download triggers.
    const boundDownload = page.waitForRequest((req) =>
      /\/api\/projects\/[^/]+\/download\?/.test(req.url()),
    );
    await panel.locator('[data-disco-control="build.download-source"]').click();
    const req = await boundDownload;

    const url = new URL(req.url());
    // The exact immutable binding of the offline `conv_demo_snake` candidate verdict.
    expect(url.searchParams.get("version_seq")).toBe("3");
    expect(url.searchParams.get("spec_digest")).toBe("sha256:demo-candidate-0001");
  });
});

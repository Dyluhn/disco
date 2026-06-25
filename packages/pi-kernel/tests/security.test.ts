/**
 * P0 security invariant: ambient provider credentials can NEVER reach the Pi
 * session and bypass the Disco inference gateway.
 *
 * Two defenses, both exercised here WITHOUT any real network call:
 *  1. `scrubProviderCredentialEnv` strips every known/likely provider key from
 *     the process env at startup, so Pi cannot read one to auto-select a real
 *     provider/model or fill a missing API key.
 *  2. The model registry exposes ONLY the Disco gateway as a usable provider —
 *     built-ins remain registered but have no credential (post-scrub), so the
 *     session selects either the gateway or no model at all, never a real
 *     provider. A startup assertion refuses to start if a built-in ever leaks.
 */
import { afterEach, describe, expect, it } from "vitest";

import {
  PiKernelRunner,
  scrubProviderCredentialEnv,
  PROVIDER_CREDENTIAL_ENV_VARS,
  GATEWAY_PROVIDER,
} from "../src/runner.ts";
import type { KernelOutbound, ReadyEvent } from "../src/protocol.ts";

/** Provider keys we plant in the env to prove they get scrubbed. */
const PLANTED = [
  "ANTHROPIC_API_KEY",
  "ANTHROPIC_OAUTH_TOKEN",
  "OPENAI_API_KEY",
  "OPENROUTER_API_KEY",
  "GEMINI_API_KEY",
  "GROQ_API_KEY",
  "XAI_API_KEY",
  "DEEPSEEK_API_KEY",
  "MISTRAL_API_KEY",
  "HF_TOKEN",
  "AWS_ACCESS_KEY_ID",
  "AWS_SECRET_ACCESS_KEY",
  "GOOGLE_APPLICATION_CREDENTIALS",
] as const;

describe("P0: scrubProviderCredentialEnv strips ambient provider credentials", () => {
  it("removes every known provider key + a future provider key, preserving unrelated env", () => {
    const env: NodeJS.ProcessEnv = {
      PATH: "/usr/bin",
      HOME: "/home/agent",
      DISCO_PI_KERNEL_MAX_LINE_BYTES: "4096",
      SOME_FUTURE_PROVIDER_API_KEY: "leak", // caught by the regex catch-all
      ACME_OAUTH_TOKEN: "leak", // caught by the regex catch-all
    };
    for (const name of PLANTED) env[name] = "secret";

    const removed = scrubProviderCredentialEnv(env);

    // All planted provider keys are gone.
    for (const name of PLANTED) {
      expect(env[name], `${name} must be scrubbed`).toBeUndefined();
    }
    // The regex catch-all caught the unknown providers too.
    expect(env.SOME_FUTURE_PROVIDER_API_KEY).toBeUndefined();
    expect(env.ACME_OAUTH_TOKEN).toBeUndefined();
    expect(removed).toContain("SOME_FUTURE_PROVIDER_API_KEY");
    expect(removed).toContain("ANTHROPIC_API_KEY");

    // Non-credential env is preserved (the sidecar still needs PATH/HOME, etc.).
    expect(env.PATH).toBe("/usr/bin");
    expect(env.HOME).toBe("/home/agent");
    expect(env.DISCO_PI_KERNEL_MAX_LINE_BYTES).toBe("4096");
  });

  it("the explicit denylist covers Pi's full provider→env map (anthropic/openai/openrouter/…)", () => {
    for (const name of [
      "ANTHROPIC_API_KEY",
      "OPENAI_API_KEY",
      "OPENROUTER_API_KEY",
      "GEMINI_API_KEY",
      "MISTRAL_API_KEY",
      "GROQ_API_KEY",
      "XAI_API_KEY",
      "DEEPSEEK_API_KEY",
    ]) {
      expect(PROVIDER_CREDENTIAL_ENV_VARS).toContain(name);
    }
  });

  it("is idempotent and a no-op on an already-clean env", () => {
    const env: NodeJS.ProcessEnv = { PATH: "/usr/bin" };
    expect(scrubProviderCredentialEnv(env)).toEqual([]);
    expect(env.PATH).toBe("/usr/bin");
  });
});

describe("P0: a Pi session cannot reach a real provider from the env", () => {
  let runner: PiKernelRunner | undefined;
  const saved = new Map<string, string | undefined>();

  function plantProviderKeys(): void {
    for (const name of PLANTED) {
      saved.set(name, process.env[name]);
      process.env[name] = "sk-fake-do-not-use";
    }
  }

  afterEach(async () => {
    if (runner) await runner.shutdown(0);
    runner = undefined;
    for (const [name, value] of saved) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    saved.clear();
  });

  it("with provider keys set in the env, init scrubs them and selects NO built-in model", async () => {
    plantProviderKeys();
    const frames: KernelOutbound[] = [];
    runner = new PiKernelRunner({ emit: (e) => frames.push(e), heartbeatMs: 10_000 });

    await runner.handleCommand({ type: "init" });

    // 1. The env vars are GONE after init (defense #1).
    for (const name of PLANTED) {
      expect(process.env[name], `${name} must be scrubbed by init`).toBeUndefined();
    }

    // 2. No real provider/model was selected — ready.model is null (defense #2).
    const ready = frames.find((f): f is ReadyEvent => f.type === "ready");
    expect(ready, "expected a ready frame").toBeDefined();
    expect(ready!.model).toBeNull();

    // 3. The live session has no REAL model: Pi's "unknown/unknown" sentinel,
    //    not any built-in provider — so a prompt cannot reach a real provider.
    const session = runner.getSession();
    expect(session, "expected a live session").toBeDefined();
    expect(session!.model?.provider ?? "unknown").toBe("unknown");

    // 4. No SECURITY refusal fired — because no built-in became usable.
    expect(frames.some((f) => f.type === "error")).toBe(false);
  });

  it("with a gateway configured, the ONLY usable model routes to the gateway (built-ins unusable despite env keys)", async () => {
    plantProviderKeys();
    const frames: KernelOutbound[] = [];
    runner = new PiKernelRunner({ emit: (e) => frames.push(e), heartbeatMs: 10_000 });

    await runner.handleCommand({
      type: "init",
      config: {
        gateway: {
          baseUrl: "http://127.0.0.1:8731/v1",
          model: "disco-build",
          apiKey: "loopback-token-from-spawner",
        },
      },
    });

    // The selected model is the gateway's, not any real provider's.
    const ready = frames.find((f): f is ReadyEvent => f.type === "ready");
    expect(ready!.model).toBe(`${GATEWAY_PROVIDER}/disco-build`);

    const session = runner.getSession();
    expect(session!.model?.provider).toBe(GATEWAY_PROVIDER);
    expect(session!.model?.baseUrl).toBe("http://127.0.0.1:8731/v1");

    // The registry exposes ONLY the gateway as usable — every other (built-in)
    // model has no credential post-scrub, so the startup assertion did not fire.
    const registry = runner.getInitOptions()!.modelRegistry!;
    const available = await registry.getAvailable();
    expect(available.length).toBeGreaterThan(0);
    expect(available.every((m) => m.provider === GATEWAY_PROVIDER)).toBe(true);

    expect(frames.some((f) => f.type === "error" && f.fatal === true)).toBe(false);
  });
});

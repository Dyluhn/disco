/**
 * C3 security invariant: the Pi provider config points ONLY at the loopback
 * Disco gateway and carries ONLY the ephemeral run token — never a real
 * provider API key. These tests exercise the pure provider-config module
 * (`discoProvider.ts`) WITHOUT any real network call:
 *
 *  1. `buildDiscoProvider` produces a config whose only credential is the
 *     ephemeral run token; no planted provider secret leaks anywhere into the
 *     object graph (mirrors `security.test.ts`).
 *  2. `registerDiscoProvider` makes `disco-gateway` the ONLY usable provider in
 *     a real in-memory registry (built-ins stay registered but unusable with no
 *     credential), and returns the gateway-pinned model.
 *  3. `api` defaults to the gateway dialect (`openai-completions`) and
 *     `baseUrl` is the loopback gateway.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { AuthStorage, ModelRegistry } from "@earendil-works/pi-coding-agent";

import {
  GATEWAY_PROVIDER,
  DEFAULT_GATEWAY_API,
  buildDiscoProvider,
  registerDiscoProvider,
  type DiscoGatewayConfig,
} from "../src/discoProvider.ts";
// The runner owns env-scrub; we reuse it ONLY to model the deployed precondition
// (ambient provider creds gone) so built-ins are deterministically unusable.
import { scrubProviderCredentialEnv } from "../src/runner.ts";

const RUN_TOKEN = "ephemeral-run-token-abc123";
const LOOPBACK = "http://127.0.0.1:8731/v1";

/** A representative config the spawner would inject. */
function cfg(overrides: Partial<DiscoGatewayConfig> = {}): DiscoGatewayConfig {
  return {
    baseUrl: LOOPBACK,
    model: "disco-build",
    apiKey: RUN_TOKEN,
    ...overrides,
  };
}

/** Provider secrets we plant to prove none leak into the config object. */
const PLANTED_SECRETS = [
  "sk-ant-real-anthropic-key",
  "sk-real-openai-key",
  "sk-or-real-openrouter-key",
  "real-gemini-key",
] as const;

/** Collect every string that appears anywhere in an arbitrary object graph. */
function collectStrings(value: unknown, out: string[] = []): string[] {
  if (typeof value === "string") {
    out.push(value);
  } else if (Array.isArray(value)) {
    for (const v of value) collectStrings(v, out);
  } else if (value && typeof value === "object") {
    for (const v of Object.values(value)) collectStrings(v, out);
  }
  return out;
}

describe("buildDiscoProvider: only the loopback gateway + ephemeral token", () => {
  it("targets the loopback gateway and carries ONLY the run token as a credential", () => {
    const config = buildDiscoProvider(cfg());

    // baseUrl is the loopback gateway, nowhere else.
    expect(config.baseUrl).toBe(LOOPBACK);
    expect(config.baseUrl!.startsWith("http://127.0.0.1")).toBe(true);

    // The sole credential is the ephemeral run token.
    expect(config.apiKey).toBe(RUN_TOKEN);

    // No OAuth/login path (which could pull real subscription creds).
    expect(config.oauth).toBeUndefined();

    // Exactly one model, pinned to the UI-selected id.
    expect(config.models).toHaveLength(1);
    expect(config.models![0]!.id).toBe("disco-build");
  });

  it("api defaults to the gateway dialect (openai-completions) at provider + model level", () => {
    const config = buildDiscoProvider(cfg());
    expect(DEFAULT_GATEWAY_API).toBe("openai-completions");
    expect(config.api).toBe("openai-completions");
    expect(config.models![0]!.api).toBe("openai-completions");
  });

  it("honors an explicit api override", () => {
    const config = buildDiscoProvider(cfg({ api: "anthropic-messages" }));
    expect(config.api).toBe("anthropic-messages");
    expect(config.models![0]!.api).toBe("anthropic-messages");
  });

  it("leaks NO planted provider secret into the config object — only the run token", () => {
    // Plant real-looking provider keys in the env; the config must be built
    // purely from the injected cfg and reference none of them.
    const saved = new Map<string, string | undefined>();
    PLANTED_SECRETS.forEach((secret, i) => {
      const name = `LEAK_PROVIDER_KEY_${i}`;
      saved.set(name, process.env[name]);
      process.env[name] = secret;
    });
    try {
      const config = buildDiscoProvider(cfg());
      const strings = collectStrings(config);

      // The ephemeral run token is the ONLY credential-shaped string present.
      const credentialShaped = strings.filter(
        (s) => s.startsWith("sk-") || s === RUN_TOKEN || PLANTED_SECRETS.includes(s as never),
      );
      expect(credentialShaped).toEqual([RUN_TOKEN]);

      // Belt-and-suspenders: no planted secret anywhere in the graph.
      for (const secret of PLANTED_SECRETS) {
        expect(strings).not.toContain(secret);
      }
    } finally {
      for (const [name, value] of saved) {
        if (value === undefined) delete process.env[name];
        else process.env[name] = value;
      }
    }
  });
});

describe("buildDiscoProvider: baseUrl is pinned to the loopback gateway", () => {
  it("accepts loopback baseUrls (127.0.0.1 / localhost / [::1])", () => {
    for (const base of [
      "http://127.0.0.1:8731/v1",
      "http://localhost:8731/v1",
      "http://[::1]:8731/v1",
    ]) {
      const config = buildDiscoProvider(cfg({ baseUrl: base }));
      expect(config.baseUrl).toBe(base);
    }
  });

  it("rejects a public (non-loopback) host", () => {
    expect(() => buildDiscoProvider(cfg({ baseUrl: "https://api.openai.com/v1" }))).toThrow(
      /loopback gateway/,
    );
    expect(() => buildDiscoProvider(cfg({ baseUrl: "http://10.0.0.5:8731/v1" }))).toThrow(
      /loopback gateway/,
    );
  });

  it("rejects a non-http scheme even on a loopback host", () => {
    // The loopback gateway speaks plain http; https://127.0.0.1 is not it.
    expect(() => buildDiscoProvider(cfg({ baseUrl: "https://127.0.0.1:8731/v1" }))).toThrow(
      /loopback gateway/,
    );
  });

  it("rejects a malformed baseUrl", () => {
    expect(() => buildDiscoProvider(cfg({ baseUrl: "not a url" }))).toThrow(/not a valid URL/);
  });
});

describe("registerDiscoProvider: disco-gateway is the ONLY usable provider", () => {
  const saved = new Map<string, string | undefined>();
  let registry: ModelRegistry;

  beforeEach(() => {
    // Model the deployed precondition: ambient provider creds scrubbed, so
    // built-in models have no configured auth and are unusable.
    const removed = scrubProviderCredentialEnv();
    for (const name of removed) saved.set(name, undefined); // already gone post-scrub
    registry = ModelRegistry.inMemory(AuthStorage.inMemory());
  });

  afterEach(() => {
    for (const [name, value] of saved) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    saved.clear();
  });

  it("registers disco-gateway and returns the gateway-pinned model", () => {
    const model = registerDiscoProvider(registry, cfg());

    expect(model, "registerDiscoProvider must return the selected model").toBeDefined();
    expect(model!.provider).toBe(GATEWAY_PROVIDER);
    expect(model!.id).toBe("disco-build");
    expect(model!.baseUrl).toBe(LOOPBACK);
  });

  it("makes disco-gateway the ONLY usable (auth-configured) provider", () => {
    registerDiscoProvider(registry, cfg());

    const available = registry.getAvailable();
    expect(available.length).toBeGreaterThan(0);
    // Every model with configured auth routes to the gateway — no real provider
    // is usable, so a prompt can reach only the loopback gateway.
    expect(available.every((m) => m.provider === GATEWAY_PROVIDER)).toBe(true);
    expect(available.some((m) => m.id === "disco-build")).toBe(true);
  });
});

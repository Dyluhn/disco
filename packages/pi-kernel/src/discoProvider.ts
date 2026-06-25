/**
 * discoProvider — the single Pi SDK provider config pointing at the loopback
 * Disco inference gateway (Disco Pi Build Kernel Campaign, EPIC C / PR C3).
 *
 * The Pi sidecar must NEVER hold a real provider API key. Instead it is given
 * exactly one usable provider — the loopback `DiscoInferenceGateway`
 * (`POST /internal/pi-kernel/v1/chat/completions`) — authenticated with an
 * ephemeral, per-run bearer token that the spawner issues and revokes. The
 * gateway pins the model by token, so the kernel can reach only the
 * UI-selected model and nothing else.
 *
 * This module is the pure, drop-in extraction of the runner's
 * `registerGatewayModel` body: `buildDiscoProvider` produces the provider
 * config object and `registerDiscoProvider` registers it and returns the
 * selected model. Keeping it standalone lets the security tests exercise the
 * credential-isolation invariants without spinning up a session, and lets the
 * runner switch to call it in a later PR.
 */
import type {
  ModelRegistry,
  ProviderConfig,
} from "@earendil-works/pi-coding-agent";

/**
 * The synthetic provider name under which the Disco inference gateway is
 * registered. It is the ONLY provider the kernel ever exposes as usable.
 */
export const GATEWAY_PROVIDER = "disco-gateway";

/**
 * Wire dialect the Disco gateway speaks. The gateway exposes the OpenAI-
 * compatible chat-completions shape, so `openai-completions` is the default
 * when a config does not pin one explicitly.
 */
export const DEFAULT_GATEWAY_API = "openai-completions";

/**
 * Config for the single loopback-gateway provider. Mirrors the stdio
 * `KernelGatewayConfig`: it carries ONLY the loopback `baseUrl` and the
 * ephemeral run-token `apiKey` — never an ambient provider credential (those
 * are scrubbed from the env at startup).
 */
export interface DiscoGatewayConfig {
  /**
   * Base URL of the loopback Disco inference gateway (e.g.
   * `http://127.0.0.1:<port>/v1`). All model traffic routes here and nowhere
   * else.
   */
  baseUrl: string;
  /** Model id exposed by the gateway and selected for this session. */
  model: string;
  /**
   * Gateway credential — the ephemeral per-run token issued by the spawner.
   * This is the ONLY credential the provider config ever carries.
   */
  apiKey: string;
  /**
   * Wire dialect the gateway speaks. Defaults to {@link DEFAULT_GATEWAY_API}.
   */
  api?: string;
}

/** Loopback hosts the gateway is reachable on — and the ONLY hosts a provider
 * baseUrl may target. */
const LOOPBACK_HOSTS: ReadonlySet<string> = new Set(["127.0.0.1", "localhost", "::1"]);

/**
 * Assert `baseUrl` points at the loopback Disco gateway and nothing else
 * (defense-in-depth for the C3 "pinned to loopback gateway" contract). Rejects
 * any non-loopback host (e.g. `https://api.openai.com/v1`) and any non-`http`
 * scheme — the loopback gateway speaks plain `http`. Throws a clear `Error` on
 * violation; returns the URL unchanged when it is loopback.
 */
function assertLoopbackBaseUrl(baseUrl: string): string {
  let url: URL;
  try {
    url = new URL(baseUrl);
  } catch {
    throw new Error(
      `discoProvider: baseUrl is not a valid URL: ${JSON.stringify(baseUrl)}`,
    );
  }
  // URL wraps IPv6 hosts in brackets ("[::1]"); strip them before comparing.
  const host = url.hostname.replace(/^\[|\]$/g, "");
  if (url.protocol !== "http:" || !LOOPBACK_HOSTS.has(host)) {
    throw new Error(
      `discoProvider: baseUrl must be the loopback gateway ` +
        `(http://{127.0.0.1,localhost,[::1]}…), got ${JSON.stringify(baseUrl)}`,
    );
  }
  return baseUrl;
}

/**
 * Build the Pi provider config for the loopback Disco gateway. The result
 * carries only `{ baseUrl, apiKey, api, models:[selected] }` — no other
 * provider credential, header, or env reference — so the sidecar can reach
 * exactly one endpoint with exactly one (ephemeral) credential.
 *
 * `baseUrl` is pinned: a non-loopback host (or non-`http` scheme) throws.
 *
 * Pure: it touches no registry, env, network, or disk.
 */
export function buildDiscoProvider(cfg: DiscoGatewayConfig): ProviderConfig {
  const api = cfg.api ?? DEFAULT_GATEWAY_API;
  const baseUrl = assertLoopbackBaseUrl(cfg.baseUrl);
  return {
    baseUrl,
    apiKey: cfg.apiKey,
    api,
    models: [
      {
        id: cfg.model,
        name: cfg.model,
        api,
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 0,
        maxTokens: 0,
      },
    ],
  };
}

/**
 * Register the Disco gateway as the sole usable provider and return its model.
 * Built-in providers remain in the registry but are never usable (no
 * credential post-scrub); only this provider carries an injected key, so it is
 * the only model `getAvailable()` returns and the only endpoint a prompt can
 * reach.
 *
 * Drop-in extraction of the runner's `registerGatewayModel` body.
 */
export function registerDiscoProvider(
  registry: ModelRegistry,
  cfg: DiscoGatewayConfig,
): ReturnType<ModelRegistry["find"]> {
  registry.registerProvider(GATEWAY_PROVIDER, buildDiscoProvider(cfg));
  return registry.find(GATEWAY_PROVIDER, cfg.model);
}

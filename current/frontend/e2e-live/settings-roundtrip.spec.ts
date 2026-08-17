import { expect, test, type APIResponse, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

import { addMcpThroughSettings } from "./mcp-reliability-helpers";

import {
  AGENT_API,
  APP_API,
  requireReliabilityStack,
} from "./reliability-helpers";

type Json = Record<string, unknown>;

const CONFIG_PATHS = [
  "/api/sandbox/config",
  "/api/encoders/config",
  "/api/tts/config",
  "/api/image-gen/config",
  "/api/data-sources/config",
  "/api/role-fallback/config",
  "/api/projects/storage/config",
  "/api/live-browser/config",
] as const;

function section(page: Page, heading: string) {
  return page
    .getByRole("heading", { name: heading, exact: true })
    .locator("xpath=ancestor::section[1]");
}

async function jsonResponse(response: APIResponse) {
  const text = await response.text();
  expect(
    response.ok(),
    `${response.url()} -> ${response.status()}: ${text}`,
  ).toBe(true);
  return text ? (JSON.parse(text) as unknown) : null;
}

async function ensureAppSession(page: Page): Promise<{ csrf_token: string }> {
  let sessionResponse = await page
    .context()
    .request.get(`${APP_API}/api/auth/session`);
  let session = (await jsonResponse(sessionResponse)) as {
    authenticated?: boolean;
    csrf_token?: string;
  };
  if (!session.authenticated || !session.csrf_token) {
    const pairingResponse = await page
      .context()
      .request.get(`${APP_API}/api/auth/pairing-token`);
    const pairing = (await jsonResponse(pairingResponse)) as {
      pairing_token?: string;
    };
    const origin = new URL(page.url()).origin;
    const minted = await page
      .context()
      .request.post(`${APP_API}/api/auth/mint`, {
        data: { pairing_token: pairing.pairing_token },
        headers: { Origin: origin },
      });
    await jsonResponse(minted);
    sessionResponse = await page
      .context()
      .request.get(`${APP_API}/api/auth/session`);
    session = (await jsonResponse(sessionResponse)) as {
      authenticated?: boolean;
      csrf_token?: string;
    };
  }
  expect(session.authenticated, "browser did not retain its App session").toBe(
    true,
  );
  expect(
    session.csrf_token,
    "App session did not expose a CSRF token",
  ).toBeTruthy();
  return { csrf_token: session.csrf_token! };
}

async function appGet<T = unknown>(page: Page, apiPath: string): Promise<T> {
  await ensureAppSession(page);
  const response = await page
    .context()
    .request.get(`${APP_API}${apiPath}`, { timeout: 30_000 });
  return (await jsonResponse(response)) as T;
}

async function appSend<T = unknown>(
  page: Page,
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  apiPath: string,
  data?: unknown,
): Promise<T> {
  const session = await ensureAppSession(page);
  const response = await page.context().request.fetch(`${APP_API}${apiPath}`, {
    method,
    data,
    headers: { "X-Disco-CSRF": session.csrf_token! },
    timeout: 60_000,
  });
  return (await jsonResponse(response)) as T;
}

async function captureConfigs(page: Page): Promise<Record<string, unknown>> {
  const entries: Array<[string, unknown]> = [];
  for (const apiPath of CONFIG_PATHS)
    entries.push([apiPath, await appGet(page, apiPath)]);
  return Object.fromEntries(entries);
}

async function restartStack(): Promise<void> {
  const control = process.env.DISCO_RELIABILITY_STACK_CONTROL_URL;
  const token = process.env.DISCO_RELIABILITY_STACK_CONTROL_TOKEN;
  expect(control, "isolated stack control URL is required").toBeTruthy();
  expect(token, "isolated stack control token is required").toBeTruthy();
  const response = await fetch(`${control}/restart`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  const text = await response.text();
  expect(
    response.ok,
    `isolated stack restart failed: ${response.status} ${text}`,
  ).toBe(true);
}

async function runProbe(
  container: ReturnType<typeof section>,
  control: string,
): Promise<{ status: string; ok: boolean }> {
  const button = container.locator(`[data-disco-control="${control}"]`);
  await expect(button).toBeEnabled();
  await button.click();
  const chip = button.locator("xpath=..").locator("[data-probe-status]");
  await expect
    .poll(async () => chip.getAttribute("data-probe-status"), {
      timeout: 60_000,
    })
    .not.toMatch(/^(idle|testing|null)$/);
  return {
    status: (await chip.getAttribute("data-probe-status")) ?? "",
    ok: (await chip.getAttribute("data-probe-ok")) === "true",
  };
}

async function bestEffortCleanup(
  page: Page,
  originalConfigs: Record<string, unknown>,
  originalAssignments: Json,
  ids: {
    model: string;
    provider?: string;
    skill?: string;
    mcp: string;
    secret: string;
  },
): Promise<void> {
  const ignore = async (operation: () => Promise<unknown>) => {
    try {
      await operation();
    } catch {
      // Preserve the primary assertion failure; the disposable stack is torn down
      // after the Playwright command even if recovery itself is impossible.
    }
  };
  await ignore(() =>
    appSend(page, "PUT", "/api/models/assignments", originalAssignments),
  );
  await ignore(() =>
    appSend(page, "DELETE", `/api/models/${encodeURIComponent(ids.model)}`),
  );
  if (ids.provider) {
    await ignore(() =>
      appSend(
        page,
        "DELETE",
        `/api/providers/${encodeURIComponent(ids.provider!)}`,
      ),
    );
  }
  if (ids.skill) {
    await ignore(() =>
      appSend(page, "DELETE", `/api/skills/${encodeURIComponent(ids.skill!)}`),
    );
  }
  await ignore(() =>
    appSend(page, "DELETE", `/api/mcp/servers/${encodeURIComponent(ids.mcp)}`),
  );
  await ignore(() =>
    appSend(page, "DELETE", `/api/secrets/${encodeURIComponent(ids.secret)}`),
  );
  await ignore(() => appSend(page, "DELETE", "/api/openrouter/key"));
  for (const apiPath of CONFIG_PATHS) {
    await ignore(() => appSend(page, "PUT", apiPath, originalConfigs[apiPath]));
  }
}

test("every Settings family applies, survives a service restart, and restores cleanly", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(600_000);
  expect(
    /^(1|true|yes)$/i.test(process.env.DISCO_RELIABILITY_ISOLATED_STACK ?? ""),
    "Settings round-trip must target the disposable App+Agent stack",
  ).toBe(true);
  await requireReliabilityStack(request);

  const stackRoot = process.env.DISCO_RELIABILITY_STACK_ROOT;
  expect(stackRoot, "isolated stack root is required").toBeTruthy();
  const suffix = `${process.pid}_${testInfo.repeatEachIndex}_${Date.now()}`;
  const modelId = `reliability-settings-${suffix}`;
  const skillName = `Reliability Settings ${suffix}`;
  const mcpName = `reliability_settings_${suffix}`.toLowerCase();
  const secretName = `RELIABILITY_SETTINGS_${suffix}`.toUpperCase();
  const providerKey = `provider-proof-${suffix}`;
  const advancedKey = `advanced-proof-${suffix}`;
  const openRouterKey = `openrouter-proof-${suffix}`;
  const alternateProjects = path.join(
    stackRoot!,
    `alternate-projects-${suffix}`,
  );
  fs.mkdirSync(alternateProjects, { recursive: true });

  await page.goto("/settings");
  await expect(
    page.getByRole("heading", { name: "Settings", exact: true }),
  ).toBeVisible();

  const originalConfigs = await captureConfigs(page);
  const originalAssignments = await appGet<Json>(
    page,
    "/api/models/assignments",
  );
  const originalModels = await appGet<Json[]>(page, "/api/models");
  const originalProviders = await appGet<Json[]>(page, "/api/providers");
  const originalSkills = await appGet<Json[]>(page, "/api/skills");
  const originalMcp = await appGet<Json[]>(page, "/api/mcp");
  const originalSecrets = await appGet<Json>(page, "/api/secrets");
  const originalOpenRouter = await appGet<Json>(page, "/api/openrouter/key");
  const originalVerbose = await page.evaluate(() =>
    localStorage.getItem("verboseAgentChat"),
  );
  let providerId: string | undefined;
  let skillId: string | undefined;

  try {
    // Local UI preference: mutate and prove a hard reload preserves it.
    const chat = section(page, "Agent chat");
    await chat
      .locator(
        '[data-disco-control="settings.verbose-chat-toggle"][data-enabled="false"]',
      )
      .click();
    expect(
      await page.evaluate(() => localStorage.getItem("verboseAgentChat")),
    ).toBe("0");

    // Generic provider CRUD performs a real /models probe. This intentionally
    // unreachable catalogue must be saved with an honest red result, never a fake green.
    const providers = section(page, "Providers");
    await providers
      .getByLabel("Provider preset")
      .selectOption("custom-openai-compatible");
    await providers
      .getByLabel("Provider API key", { exact: true })
      .fill(providerKey);
    await providers
      .getByLabel("Custom provider base URL")
      .fill(`${APP_API}/reliability-provider-${suffix}`);
    await providers
      .locator('[data-disco-control="settings.provider-add"]')
      .click();
    await expect(
      providers.getByRole("alert").filter({ hasText: "was saved, but" }),
    ).toBeVisible({
      timeout: 60_000,
    });
    const createdProviders = await appGet<Json[]>(page, "/api/providers");
    const createdProvider = createdProviders.find(
      (item) => item.base_url === `${APP_API}/reliability-provider-${suffix}`,
    );
    expect(
      createdProvider,
      "provider UI did not persist the custom endpoint",
    ).toBeTruthy();
    providerId = String(createdProvider!.id);
    expect(createdProvider).not.toHaveProperty("api_key");

    // Dedicated OpenRouter and generic named-key stores are separate controls;
    // exercise both and insist that their fake plaintext never appears on disk.
    await providers.getByLabel("OpenRouter API key").fill(openRouterKey);
    await providers
      .locator('[data-disco-control="settings.openrouter-key-save"]')
      .click();
    await expect(
      providers.getByText("Key configured", { exact: false }),
    ).toBeVisible();
    const openRouterProbe = await runProbe(
      providers,
      "settings.openrouter-key-test",
    );
    expect(
      openRouterProbe.ok,
      "a deliberately invalid OpenRouter key got a false green",
    ).toBe(false);

    await providers
      .getByText("Advanced provider keys", { exact: true })
      .click();
    await providers.getByLabel("Provider key name").fill(secretName);
    await providers.getByLabel("Provider key value").fill(advancedKey);
    await providers
      .locator('[data-disco-control="settings.provider-key-add"]')
      .click();
    await expect(
      providers.getByText(secretName, { exact: true }),
    ).toBeVisible();

    // Catalogue CRUD + a role assignment verifies that a saved model becomes
    // immediately selectable by the real role matrix.
    const catalogue = section(page, "Catalogue");
    await catalogue
      .locator('[data-disco-control="settings.model-add"]')
      .click();
    const modelDialog = page.getByRole("dialog", { name: "Add a model" });
    await modelDialog.getByLabel("Catalogue id").fill(modelId);
    await modelDialog
      .getByLabel("Model id (sent to the API)")
      .fill(`upstream-${suffix}`);
    await modelDialog.getByLabel("Endpoint base URL").fill(`${APP_API}/v1`);
    await modelDialog
      .getByText("Advanced model metadata", { exact: true })
      .click();
    await modelDialog.getByLabel("Context window").fill("32768");
    await modelDialog.getByText("Tool calling", { exact: true }).click();
    await modelDialog
      .getByRole("button", { name: "Add model", exact: true })
      .click();
    await expect(
      catalogue.locator(`[data-model-id="${modelId}"]`).first(),
    ).toBeVisible();

    const matrix = section(page, "Role assignments");
    await matrix
      .getByText("Specialist role overrides", { exact: true })
      .click();
    await matrix.getByLabel("Choose model for Query rewriter").click();
    await page
      .locator(
        `[data-disco-control="settings.model-assign-option"][data-model-id="${modelId}"]`,
      )
      .click();
    await expect
      .poll(async () => {
        const assignments = await appGet<Json>(page, "/api/models/assignments");
        return (assignments.roles as Json).query_rewriter;
      })
      .toBe(modelId);

    // Every intelligence setting gets a non-default value. Its real consumer
    // probes must return a typed verdict; unreachable fixtures are expected red.
    await page
      .getByText("Advanced model resilience", { exact: true })
      .click();
    const resilience = section(page, "Model resilience");
    await resilience
      .getByText("Fall back to a local model for auxiliary roles", {
        exact: true,
      })
      .click();
    await resilience
      .getByLabel("Endpoint base URL", { exact: false })
      .fill(`${APP_API}/fallback/v1`);
    await resilience
      .getByLabel("Model", { exact: true })
      .fill(`fallback-${suffix}`);
    await resilience
      .getByLabel("Fallback credential", { exact: true })
      .fill(secretName);
    await resilience
      .locator('[data-disco-control="settings.role-fallback-save"]')
      .click();

    const image = section(page, "Image generation");
    await image
      .locator(
        '[data-disco-control="settings.imagegen-provider"][data-provider="comfyui"]',
      )
      .click();
    await image
      .getByLabel("Endpoint base URL", { exact: false })
      .fill(`${APP_API}/comfy-${suffix}`);
    await image
      .getByLabel("Checkpoint", { exact: false })
      .fill(`checkpoint-${suffix}.safetensors`);
    await image
      .locator('[data-disco-control="settings.imagegen-save"]')
      .click();
    const imageProbe = await runProbe(image, "settings.imagegen-test");
    expect(imageProbe.ok, "unreachable ComfyUI received a false green").toBe(
      false,
    );

    const encoders = section(page, "Encoders");
    await encoders.getByText("Configure encoders", { exact: true }).click();
    await encoders
      .locator(
        '[data-disco-control="settings.encoder-mode"][data-remote="true"]',
      )
      .click();
    await encoders
      .locator('[data-endpoint="embedder_url"]')
      .fill(`${APP_API}/embed-${suffix}`);
    await encoders
      .locator('[data-endpoint="reranker_url"]')
      .fill(`${APP_API}/rerank-${suffix}`);
    await encoders
      .locator('[data-endpoint="nli_url"]')
      .fill(`${APP_API}/nli-${suffix}`);
    await encoders
      .locator('[data-disco-control="settings.encoder-save"]')
      .click();

    const sources = section(page, "Data sources");
    await sources.getByText("Configure search", { exact: true }).click();
    await sources.getByText("Configure extraction", { exact: true }).click();
    await sources.locator('[data-provider-id="news"]').click();
    await sources.locator('[data-provider-id="crawl4ai"]').click();
    await sources.getByLabel("Service URL").fill(`${APP_API}/crawl-${suffix}`);
    await sources
      .locator('[data-disco-control="settings.datasource-save"]')
      .click();
    await runProbe(sources, "settings.datasource-test-search");
    const extractionProbe = await runProbe(
      sources,
      "settings.datasource-test-extraction",
    );
    expect(
      extractionProbe.ok,
      "unreachable extraction endpoint received a false green",
    ).toBe(false);

    const audio = section(page, "Audio overview");
    await audio
      .locator(
        '[data-disco-control="settings.audio-mode"][data-mode="speaches"]',
      )
      .click();
    await audio
      .getByLabel("Endpoint base URL", { exact: false })
      .fill(`${APP_API}/speech-${suffix}`);
    await audio
      .getByLabel("Host A voice", { exact: false })
      .fill(`host-a-${suffix}`);
    await audio
      .getByLabel("Host B voice", { exact: true })
      .fill(`host-b-${suffix}`);
    await audio.locator('[data-disco-control="settings.audio-save"]').click();
    const audioProbe = await runProbe(audio, "settings.audio-test-tts");
    expect(
      audioProbe.ok,
      "unreachable speech endpoint received a false green",
    ).toBe(false);

    // Sandbox save runs its real preflight. The local Docker socket may or may
    // not exist on a given fresh device; either typed result is valid evidence.
    const sandbox = section(page, "Sandbox");
    await sandbox.getByLabel("Use the Local container sandbox backend").click();
    await sandbox
      .locator('[data-disco-control="settings.sandbox-save"]')
      .click();
    await expect(sandbox.locator("[data-sandbox-probe]")).toBeVisible({
      timeout: 60_000,
    });

    await expect(
      page.locator('[data-live-browser-relevance="gvisor-only"]'),
    ).toBeVisible();
    expect((await appGet<Json>(page, "/api/live-browser/config")).enabled).toBe(
      false,
    );

    const storage = section(page, "Project storage");
    await storage.getByLabel("Projects root").fill(alternateProjects);
    await storage
      .locator('[data-disco-control="settings.storage-save"]')
      .click();
    await expect(
      storage.locator('[data-storage-validation="ok"]'),
    ).toBeVisible();

    // File-backed skills prove CRUD, per-surface scoping, and disabled state.
    const skills = section(page, "Skills");
    await skills.locator('[data-disco-control="settings.skill-new"]').click();
    await skills.getByLabel("Skill name").fill(skillName);
    await skills
      .getByLabel("Skill description")
      .fill("Settings restart evidence");
    await skills
      .getByLabel("Skill instructions (markdown)")
      .fill(`# ${skillName}\n\nReturn SETTINGS_SKILL_OK.`);
    await skills.getByLabel("Agent surface").click();
    await skills.locator('[data-disco-control="settings.skill-save"]').click();
    const skillRow = skills.locator("li").filter({ hasText: skillName });
    await expect(skillRow).toBeVisible();
    await skillRow.getByLabel(`Enable ${skillName}`).click();
    const createdSkills = await appGet<Json[]>(page, "/api/skills");
    const createdSkill = createdSkills.find((item) => item.name === skillName);
    expect(createdSkill).toMatchObject({ enabled: false, surfaces: ["build"] });
    skillId = String(createdSkill!.id);

    // The full enabled MCP call path has its own workflow suite. Here we cover
    // Settings CRUD/hot reload/restart with a disabled connection, so no external
    // server or personal MCP configuration is needed.
    const mcp = section(page, "Connections (MCP)");
    await addMcpThroughSettings(page, {
      name: mcpName,
      url: `${APP_API}/mcp-${suffix}`,
    });
    const mcpRow = mcp.locator("li").filter({ hasText: mcpName });
    await expect(mcpRow).toBeVisible({ timeout: 60_000 });
    await mcpRow.getByLabel(`Enable ${mcpName}`).click();
    await expect
      .poll(async () => {
        const entries = await appGet<Json[]>(page, "/api/mcp");
        return entries.find((item) => item.name === mcpName)?.enabled;
      })
      .toBe(false);

    const mutatedConfigs = await captureConfigs(page);
    const mutatedModels = await appGet<Json[]>(page, "/api/models");
    const mutatedAssignments = await appGet<Json>(
      page,
      "/api/models/assignments",
    );
    const mutatedProviders = await appGet<Json[]>(page, "/api/providers");
    const mutatedSkills = await appGet<Json[]>(page, "/api/skills");
    const mutatedMcp = await appGet<Json[]>(page, "/api/mcp");
    const mutatedSecrets = await appGet<Json>(page, "/api/secrets");
    const mutatedOpenRouter = await appGet<Json>(page, "/api/openrouter/key");
    expect(mutatedMcp.find((item) => item.name === mcpName)).toMatchObject({
      enabled: false,
      transport: "streamable_http",
      risk_tier: "low",
    });

    const secretDisk = fs.readFileSync(
      path.join(stackRoot!, "secrets.json"),
      "utf-8",
    );
    const configDisk = fs.readFileSync(
      path.join(stackRoot!, "disco-config.json"),
      "utf-8",
    );
    for (const plaintext of [providerKey, advancedKey, openRouterKey]) {
      expect(secretDisk, "secret store leaked plaintext").not.toContain(
        plaintext,
      );
      expect(configDisk, "config store leaked plaintext").not.toContain(
        plaintext,
      );
    }

    // Restart both services, not merely the browser. The same DTOs, IDs, encrypted
    // key statuses, and file-backed skill must come back byte-for-byte equivalent.
    await restartStack();
    await page.reload();
    await expect(
      page.getByRole("heading", { name: "Settings", exact: true }),
    ).toBeVisible();
    expect(await captureConfigs(page)).toEqual(mutatedConfigs);
    expect(await appGet<Json[]>(page, "/api/models")).toEqual(mutatedModels);
    expect(await appGet<Json>(page, "/api/models/assignments")).toEqual(
      mutatedAssignments,
    );
    expect(await appGet<Json[]>(page, "/api/providers")).toEqual(
      mutatedProviders,
    );
    expect(await appGet<Json[]>(page, "/api/skills")).toEqual(mutatedSkills);
    expect(await appGet<Json>(page, "/api/secrets")).toEqual(mutatedSecrets);
    expect(await appGet<Json>(page, "/api/openrouter/key")).toEqual(
      mutatedOpenRouter,
    );
    const restartedMcp = await appGet<Json[]>(page, "/api/mcp");
    expect(restartedMcp.find((item) => item.name === mcpName)).toMatchObject({
      url: `${APP_API}/mcp-${suffix}`,
      enabled: false,
      transport: "streamable_http",
      risk_tier: "low",
    });

    expect(
      await page.evaluate(() => localStorage.getItem("verboseAgentChat")),
    ).toBe("0");
    await page
      .getByText("Advanced model resilience", { exact: true })
      .click();
    await section(page, "Encoders")
      .getByText("Configure encoders", { exact: true })
      .click();
    await section(page, "Data sources")
      .getByText("Configure search", { exact: true })
      .click();
    await expect(
      section(page, "Model resilience").locator(
        '[data-disco-control="settings.role-fallback-toggle"]',
      ),
    ).toBeChecked();
    await expect(
      section(page, "Image generation").locator('[data-provider="comfyui"]'),
    ).toHaveAttribute("aria-pressed", "true");
    await expect(
      section(page, "Encoders").locator('[data-remote="true"]'),
    ).toHaveAttribute("aria-pressed", "true");
    await expect(
      section(page, "Data sources").locator('[data-provider-id="news"]'),
    ).toHaveAttribute("aria-pressed", "true");
    await expect(
      section(page, "Audio overview").locator('[data-mode="speaches"]'),
    ).toHaveAttribute("aria-pressed", "true");
    await expect(
      section(page, "Sandbox").getByLabel(
        "Use the Local container sandbox backend",
      ),
    ).toHaveAttribute("aria-checked", "true");
    await expect(
      section(page, "Project storage").getByLabel("Projects root"),
    ).toHaveValue(alternateProjects);
    await expect(page.getByText(skillName, { exact: true })).toBeVisible();
    await expect(page.getByText(mcpName, { exact: true })).toBeVisible();
    await expect(
      page.locator(`[data-model-id="${modelId}"]`).first(),
    ).toBeVisible();

    // Exercise UI deletion paths before restoring the scalar DTO snapshots.
    const restartedMatrix = section(page, "Role assignments");
    await restartedMatrix
      .getByText("Specialist role overrides", { exact: true })
      .click();
    await restartedMatrix
      .getByLabel("Choose model for Query rewriter")
      .click();
    const originalQueryModel = String(
      (originalAssignments.roles as Json).query_rewriter,
    );
    await page
      .locator(
        `[data-disco-control="settings.model-assign-option"][data-model-id="${originalQueryModel}"]`,
      )
      .click();
    await section(page, "Catalogue").getByLabel(`Remove ${modelId}`).click();
    await page
      .getByRole("button", { name: "Remove model", exact: true })
      .click();
    await expect
      .poll(async () =>
        (await appGet<Json[]>(page, "/api/models")).some(
          (m) => m.id === modelId,
        ),
      )
      .toBe(false);

    const currentProvider = (await appGet<Json[]>(page, "/api/providers")).find(
      (p) => p.id === providerId,
    )!;
    await section(page, "Providers")
      .getByLabel(`Delete ${String(currentProvider.label)}`)
      .click();
    await expect
      .poll(async () =>
        (await appGet<Json[]>(page, "/api/providers")).some(
          (p) => p.id === providerId,
        ),
      )
      .toBe(false);

    await section(page, "Skills").getByLabel(`Delete ${skillName}`).click();
    await section(page, "Connections (MCP)")
      .getByLabel(`Remove ${mcpName}`)
      .click();
    const providerSection = section(page, "Providers");
    await providerSection
      .locator('[data-disco-control="settings.openrouter-key-clear"]')
      .click();
    await providerSection
      .getByText("Advanced provider keys", { exact: true })
      .click();
    const keyRow = providerSection
      .locator("li")
      .filter({ hasText: secretName });
    await keyRow
      .locator('[data-disco-control="settings.provider-key-clear"]')
      .click();

    for (const apiPath of CONFIG_PATHS) {
      await appSend(page, "PUT", apiPath, originalConfigs[apiPath]);
    }
    if (originalVerbose === null) {
      await page.evaluate(() => localStorage.removeItem("verboseAgentChat"));
    } else {
      await page.evaluate(
        (value) => localStorage.setItem("verboseAgentChat", value),
        originalVerbose,
      );
    }

    await restartStack();
    await page.reload();
    await expect(
      page.getByRole("heading", { name: "Settings", exact: true }),
    ).toBeVisible();
    expect(await captureConfigs(page)).toEqual(originalConfigs);
    expect(await appGet<Json[]>(page, "/api/models")).toEqual(originalModels);
    expect(await appGet<Json>(page, "/api/models/assignments")).toEqual(
      originalAssignments,
    );
    expect(await appGet<Json[]>(page, "/api/providers")).toEqual(
      originalProviders,
    );
    expect(await appGet<Json[]>(page, "/api/skills")).toEqual(originalSkills);
    expect(await appGet<Json[]>(page, "/api/mcp")).toEqual(originalMcp);
    expect(await appGet<Json>(page, "/api/secrets")).toEqual(originalSecrets);
    expect(await appGet<Json>(page, "/api/openrouter/key")).toEqual(
      originalOpenRouter,
    );

    // The agent also restarted and retained inspect mode; this catches a partial
    // restart where only the App server came back.
    const agentHealth = await page.context().request.get(`${AGENT_API}/health`);
    expect(agentHealth.ok()).toBe(true);
    const inspect = await page
      .context()
      .request.get(`${AGENT_API}/api/debug/inspect`);
    expect((await jsonResponse(inspect)) as Json).toMatchObject({
      enabled: true,
    });
  } finally {
    await bestEffortCleanup(page, originalConfigs, originalAssignments, {
      model: modelId,
      provider: providerId,
      skill: skillId,
      mcp: mcpName,
      secret: secretName,
    });
    if (originalVerbose === null) {
      await page
        .evaluate(() => localStorage.removeItem("verboseAgentChat"))
        .catch(() => undefined);
    } else {
      await page
        .evaluate(
          (value) => localStorage.setItem("verboseAgentChat", value),
          originalVerbose,
        )
        .catch(() => undefined);
    }
  }
});

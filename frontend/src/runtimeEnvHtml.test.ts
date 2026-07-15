import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const root = resolve(import.meta.dirname, "..");
const indexHtml = readFileSync(resolve(root, "index.html"), "utf8");
const entrypoint = readFileSync(resolve(root, "docker-entrypoint.d/40-disco-env.sh"), "utf8");
const publicBootstrap = readFileSync(resolve(root, "public/env.js"), "utf8");

describe("runtime env bootstrap", () => {
  it("keeps the deploy-generated classic script external to Vite's module graph", () => {
    const envScript = '<script vite-ignore src="/env.js"></script>';
    const appScript = '<script type="module" src="/src/main.tsx"></script>';

    expect(indexHtml).toContain(envScript);
    expect(indexHtml.indexOf(envScript)).toBeLessThan(indexHtml.indexOf(appScript));
    expect(entrypoint).toContain("/usr/share/nginx/html/env.js");
    expect(entrypoint).toContain("window.__DISCO_ENV");
  });

  it("ships a safe Vite bootstrap that production replaces with runtime config", () => {
    expect(publicBootstrap).toContain("window.__DISCO_ENV = {};");
    expect(publicBootstrap).not.toMatch(/API_BASE\s*:/);
    expect(publicBootstrap).not.toMatch(/AGENT_BASE\s*:/);

    expect(entrypoint).toContain('API_BASE:   "${PUBLIC_API}"   || "/svc/app"');
    expect(entrypoint).toContain('AGENT_BASE: "${PUBLIC_AGENT}" || "/svc/agent"');
  });
});

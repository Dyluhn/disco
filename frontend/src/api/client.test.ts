import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";


function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function mintBody(init: RequestInit | undefined): { pairing_token?: string } {
  if (!init?.body || typeof init.body !== "string") return {};
  return JSON.parse(init.body) as { pairing_token?: string };
}

describe("client auth mint pairing", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubGlobal("__DISCO_ENV", { API_BASE: "http://app" });
  });

  afterEach(() => {
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
  });

  it("prompts for a token after remote mint returns loopback_required", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "http://app/api/auth/session") {
        return jsonResponse({ authenticated: false });
      }
      if (url === "http://app/api/auth/mint") {
        const body = mintBody(init);
        if (body.pairing_token === "remote-token") {
          return jsonResponse({ csrf_token: "csrf-remote" });
        }
        return jsonResponse({ detail: { reason: "loopback_required" } }, 403);
      }
      if (url === "http://app/api/projects/storage") {
        return jsonResponse({ ok: true });
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { apiSend, ensureApiSession } = await import("@/api/client");
    const pending = ensureApiSession();

    await waitFor(() => {
      expect(screen.getByRole("dialog", { name: /pair this browser/i })).toBeInTheDocument();
    });
    await userEvent.type(screen.getByLabelText(/one-time pairing token/i), "remote-token");
    await userEvent.click(screen.getByRole("button", { name: "Pair" }));
    await pending;

    await apiSend("POST", "/api/projects/storage", { ok: true });
    const projectCall = fetchMock.mock.calls.find(
      ([url]) => String(url) === "http://app/api/projects/storage",
    );
    expect(projectCall).toBeDefined();
    expect(new Headers(projectCall?.[1]?.headers).get("X-Disco-CSRF")).toBe("csrf-remote");
    expect(screen.queryByRole("dialog", { name: /pair this browser/i })).not.toBeInTheDocument();
  });

  it("prompts when loopback pairing-token auto-fetch is unavailable", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "http://app/api/auth/session") {
        return jsonResponse({ authenticated: false });
      }
      if (url === "http://app/api/auth/mint") {
        const body = mintBody(init);
        if (body.pairing_token === "manual-token") {
          return jsonResponse({ csrf_token: "csrf-manual" });
        }
        return jsonResponse({ detail: { reason: "pairing_required" } }, 401);
      }
      if (url === "http://app/api/auth/pairing-token") {
        return jsonResponse({ detail: { reason: "loopback_required" } }, 403);
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { ensureApiSession } = await import("@/api/client");
    const pending = ensureApiSession();

    await waitFor(() => {
      expect(screen.getByRole("dialog", { name: /pair this browser/i })).toBeInTheDocument();
    });
    await userEvent.type(screen.getByLabelText(/one-time pairing token/i), "manual-token");
    await userEvent.click(screen.getByRole("button", { name: "Pair" }));

    await expect(pending).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://app/api/auth/pairing-token",
      expect.objectContaining({ credentials: "include" }),
    );
  });
});

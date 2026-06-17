/**
 * ProviderKeysSection live test — encrypt-all-keys S4.
 *
 * Drives the REAL data path: ProviderKeysSection → useSecrets/useSetSecret →
 * @/api/secrets → @/api/client → fetch(...). Only the network boundary is
 * stubbed (vi.stubGlobal("fetch")), same seam as ProjectStorageSection.test.tsx.
 * NO vi.mock of the data layer (that would let broken wiring pass).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as clientModule from "@/api/client";
import { ProviderKeysSection } from "./ProviderKeysSection";

let names: string[];
let lockedFlag = false;

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function installFetch() {
  const stub = vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "GET" && url === "/api/secrets") {
      return jsonResponse({ names: [...names].sort(), locked: lockedFlag, can_store: true });
    }
    if (method === "PUT" && url.startsWith("/api/secrets/")) {
      const name = decodeURIComponent(url.split("/api/secrets/")[1]);
      names.push(name);
      return jsonResponse({ name, configured: true, locked: false, can_store: true });
    }
    if (method === "DELETE" && url.startsWith("/api/secrets/")) {
      const name = decodeURIComponent(url.split("/api/secrets/")[1]);
      names = names.filter((n) => n !== name);
      return jsonResponse({ name, configured: false, locked: false, can_store: true });
    }
    throw new Error(`unexpected fetch: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", stub);
  return stub;
}

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

let fetchStub: ReturnType<typeof vi.fn>;

beforeEach(() => {
  names = [];
  lockedFlag = false;
  vi.spyOn(clientModule, "isLive").mockReturnValue(true);
  fetchStub = installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ProviderKeysSection — store any provider key encrypted by name", () => {
  it("PUTs a new key by name and shows it in the stored list", async () => {
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });
    await screen.findByText("Provider API keys");

    fireEvent.change(screen.getByLabelText("Provider key env-var name"), {
      target: { value: "OPENAI_API_KEY" },
    });
    fireEvent.change(screen.getByLabelText("Provider key value"), {
      target: { value: "sk-openai-SECRET" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add key/i }));

    // the PUT fired against the real fetch path with the name in the URL...
    await waitFor(() =>
      expect(
        fetchStub.mock.calls.some(
          ([u, i]) =>
            (i?.method ?? "GET").toUpperCase() === "PUT" && u === "/api/secrets/OPENAI_API_KEY",
        ),
      ).toBe(true),
    );
    // ...the value rode in the body, NOT the URL (write-only key)...
    const putCall = fetchStub.mock.calls.find(
      ([u, i]) => (i?.method ?? "GET").toUpperCase() === "PUT",
    );
    expect(String(putCall?.[1]?.body)).toContain("sk-openai-SECRET");
    // ...and the stored name now renders in the list.
    expect(await screen.findByText("OPENAI_API_KEY")).toBeInTheDocument();
  });

  it("rejects a non-env-var name inline without hitting the network", async () => {
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });
    await screen.findByText("Provider API keys");

    fireEvent.change(screen.getByLabelText("Provider key env-var name"), {
      target: { value: "bad name!" },
    });
    fireEvent.change(screen.getByLabelText("Provider key value"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: /add key/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/env-var name/i);
    expect(
      fetchStub.mock.calls.some(([, i]) => (i?.method ?? "GET").toUpperCase() === "PUT"),
    ).toBe(false);
  });

  it("clears a stored key via DELETE", async () => {
    names = ["TAVILY_API_KEY"];
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });
    expect(await screen.findByText("TAVILY_API_KEY")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /clear/i }));
    await waitFor(() =>
      expect(
        fetchStub.mock.calls.some(
          ([u, i]) =>
            (i?.method ?? "GET").toUpperCase() === "DELETE" &&
            u === "/api/secrets/TAVILY_API_KEY",
        ),
      ).toBe(true),
    );
  });

  it("edits a stored key in place (PUT with the new value)", async () => {
    names = ["OPENAI_API_KEY"];
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });
    expect(await screen.findByText("OPENAI_API_KEY")).toBeInTheDocument();

    // reveal the inline edit input, type a new value, save
    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    fireEvent.change(screen.getByLabelText("New value for OPENAI_API_KEY"), {
      target: { value: "sk-openai-ROTATED" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => {
      const put = fetchStub.mock.calls.find(
        ([u, i]) =>
          (i?.method ?? "GET").toUpperCase() === "PUT" && u === "/api/secrets/OPENAI_API_KEY",
      );
      expect(put).toBeTruthy();
      expect(String(put?.[1]?.body)).toContain("sk-openai-ROTATED");
    });
  });

  it("surfaces a loud locked alert when keys can't be decrypted", async () => {
    lockedFlag = true;
    names = ["OPENAI_API_KEY"];
    render(createElement(ProviderKeysSection), { wrapper: makeWrapper() });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/can't be decrypted/i);
    expect(alert).toHaveTextContent(/DISCO_SECRET_KEY/);
  });
});

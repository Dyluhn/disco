/**
 * Deliverable handoff: when a build FINISHES and there's a renderable artifact,
 * the inspector auto-switches to the Preview tab so the user sees the result —
 * but only on the rising edge into FINISHED (never yanking mid-run).
 *
 * E2 + E3 — PreviewPane: a bundler entry's srcdoc is unrunnable (it references
 * /src/ or /@vite/ modules the iframe can't load), so the live server is
 * preferred by default when available (E2); the cross-browser Open-in-new-tab
 * path route is exposed for Firefox / Safari (E3).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ExecutionCanvas } from "@/components/build/ExecutionCanvas";
import { FilesPane } from "@/components/build/canvas/FilesPane";
import { PreviewPane } from "@/components/build/canvas/PreviewPane";
import { previewHostUrl } from "@/api/client";
import type { AgentEvent, PreviewInfo } from "@/types/agent";

// ---- Mocks for E2 / E3 tests (also make the existing tests deterministic) --
//
// E2 / E3 tests need `proxyAvailable === true`, which the real useBuildPreview
// never produces in fixture mode (no AGENT_BASE → getPreview returns
// available:false). vi.hoisted runs before any imports, so the mock reference
// is safe inside the vi.mock factory below.
const { useBuildPreviewMock } = vi.hoisted(() => ({
  useBuildPreviewMock: vi.fn<[{ data: PreviewInfo | null }]>(() => ({ data: null })),
}));

vi.mock("@/hooks/useBuildPreview", () => ({
  useBuildPreview: (...args: unknown[]) => useBuildPreviewMock(...args),
}));

// E3 tests assert the absolute URL of the Open-in-new-tab link. The real
// `agentHttpBase()` is module-level and depends on the deployer /env.js; for
// deterministic tests we pin it to a known base. Everything else from
// @/api/client (previewHostUrl, isLive, etc.) stays real.
vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return {
    ...actual,
    agentHttpBase: () => "http://agent.test:8000",
  };
});

describe("previewHostUrl (DC-01)", () => {
  it("returns null when base is unconfigured", () => {
    expect(previewHostUrl("conv_12345678", 8000, "")).toBeNull();
  });

  it("extracts cid8 and maps 127.0.0.1 to .localhost", () => {
    expect(previewHostUrl("conv_abcdef123456", 8000, "http://127.0.0.1:8000")).toBe("http://abcdef12-8000.localhost:8000");
  });

  it("extracts cid8 and maps localhost to .localhost", () => {
    expect(previewHostUrl("conv_abcdef123456", 5173, "http://localhost:8000")).toBe("http://abcdef12-5173.localhost:8000");
  });

  it("prepends cid8-port to other hostnames", () => {
    expect(previewHostUrl("conv_11112222", 8000, "http://my-magic-dns.net")).toBe("http://11112222-8000.my-magic-dns.net");
    expect(previewHostUrl("conv_11112222", 8000, "https://my-magic-dns.net:443")).toBe("https://11112222-8000.my-magic-dns.net");
  });
});

// The Preview pane uses useBuildPreview (react-query), so renders need a client.
function withClient(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

function fileWrite(path: string, content: string): AgentEvent {
  return {
    id: `w-${path}`,
    kind: "action",
    thought: "",
    tool_call: { tool_name: "file_write", arguments: { path, content } },
  } as AgentEvent;
}

const HTML = fileWrite("index.html", "<html><body><h1>Done</h1></body></html>");
const NOTE = fileWrite("notes.txt", "no html here");

/** A minimal Vite/bundler index.html: the <script type="module" src="/src/...">
 *  reference makes the srcdoc iframe unrunnable (E2 detection). */
const BUNDLER_HTML = fileWrite(
  "index.html",
  `<!doctype html><html><body><div id="root"></div>
<script type="module" src="/src/main.tsx"></script>
</body></html>`,
);

describe("ExecutionCanvas — auto-switch to Preview on finish", () => {
  it("switches to Preview when the run finishes with a renderable artifact", () => {
    // start mid-run with a non-renderable file → Preview is NOT the initial tab
    const { rerender } = render(
      withClient(<ExecutionCanvas events={[NOTE]} status="RUNNING" cid="c1" streamingFile={null} />),
    );
    expect(screen.getByRole("tab", { name: /preview/i })).toHaveAttribute(
      "aria-selected",
      "false",
    );
    // the html artifact lands and the run finishes → auto-switch to Preview
    rerender(
      withClient(
        <ExecutionCanvas events={[NOTE, HTML]} status="FINISHED" cid="c1" streamingFile={null} />,
      ),
    );
    expect(screen.getByRole("tab", { name: /preview/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("does not force Preview on finish when there's nothing renderable", () => {
    const { rerender } = render(
      withClient(<ExecutionCanvas events={[NOTE]} status="RUNNING" cid="c2" streamingFile={null} />),
    );
    rerender(
      withClient(<ExecutionCanvas events={[NOTE]} status="FINISHED" cid="c2" streamingFile={null} />),
    );
    expect(screen.getByRole("tab", { name: /preview/i })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });
});

describe("ExecutionCanvas — untrusted preview hardening (rp-06 import)", () => {
  it("keeps allow-scripts for a trusted (own) run", () => {
    render(
      withClient(<ExecutionCanvas events={[NOTE, HTML]} status="FINISHED" cid="c3" />),
    );
    expect(screen.getByTitle("Static preview")).toHaveAttribute("sandbox", "allow-scripts");
  });

  it("drops allow-scripts (empty sandbox) for an untrusted shared/imported run", () => {
    render(
      withClient(
        <ExecutionCanvas events={[NOTE, HTML]} status="FINISHED" cid={null} untrusted />,
      ),
    );
    // empty sandbox attr = no scripts: a script here could reach open-CORS APIs
    expect(screen.getByTitle("Static preview")).toHaveAttribute("sandbox", "");
    expect(screen.getByText(/Scripted preview is disabled/i)).toBeInTheDocument();
  });
});

// ---- E2 — bundler entry + live proxy → default to the live server, not srcdoc
//
// The srcDoc iframe cannot resolve `/src/main.tsx` or `/@vite/client` (those
// are dev-server virtual modules). Showing the user a broken srcdoc when the
// dev server is up is a false affordance — they think the build is broken when
// it's actually fine. Default to the live server for bundler entries.

describe("PreviewPane — E2: bundler entry defaults to live server when proxy is available", () => {
  it("shows the live iframe (not the broken srcdoc) by default for a Vite entry", () => {
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: true,
        owner: { pid: 4242, cmdline: "vite --port 5173", session: "dev" },
        ports: [
          { port: 5173, owner: { pid: 4242, cmdline: "vite --port 5173", session: "dev" } },
        ],
      } as PreviewInfo,
    });
    render(
      withClient(
        <ExecutionCanvas events={[BUNDLER_HTML]} status="RUNNING" cid="conv_e2bundler" />,
      ),
    );
    // The bundler entry is detected → live iframe is the default, NOT the srcdoc.
    expect(screen.getByTitle("Live preview")).toBeInTheDocument();
    expect(screen.queryByTitle("Static preview")).not.toBeInTheDocument();
    // The header is the live-server header, not the preview header.
    expect(screen.getByText("live server")).toBeInTheDocument();
  });

  it("defaults to RENDERED for plain static HTML even when a proxy is available (2026-07-07 walkthrough)", () => {
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: true,
        owner: { pid: 5151, cmdline: "python -m http.server 8000", session: "preview" },
        ports: [
          { port: 8000, owner: { pid: 5151, cmdline: "python -m http.server 8000", session: "preview" } },
        ],
      } as PreviewInfo,
    });
    render(
      withClient(<ExecutionCanvas events={[HTML]} status="RUNNING" cid="conv_e2static" />),
    );
    // 2026-07-07 (supersedes runthru-v2 #4): static HTML defaults to the
    // always-correct RENDERED view; auto-preferring live landed users on a
    // half-booted/blank dev server. Live stays one click away via the toggle.
    // Only bundler entries (broken srcdoc) and srcDoc==null still default live.
    expect(screen.getByTitle("Static preview")).toBeInTheDocument();
    expect(screen.queryByTitle("Live preview")).not.toBeInTheDocument();
    // The live proxy is available, so the "Live" toggle is offered.
    expect(screen.getByRole("button", { name: /live/i })).toBeInTheDocument();
  });
});

// ---- E3 — Firefox-safe Open-in-new-tab link to the agent-server path route -
//
// The origin-true `{cid8}-{port}.localhost` URL is the most correct, but
// Firefox won't resolve `.localhost` subdomains out of the box. The
// agent-server also exposes a same-origin `/conversations/{cid}/preview-app/`
// path route that proxies the same content. Surface it as a link so Firefox
// / Safari / non-magic-DNS users have an escape hatch (do not remove the
// iframe — Chrome users get the correct asset origins there).

describe("PreviewPane — E3: Open-in-new-tab link to the /preview-app/ path route", () => {
  it("renders a link pointing at the /preview-app/ path route when the proxy is up", () => {
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: true,
        owner: { pid: 4242, cmdline: "vite --port 5173", session: "dev" },
        ports: [
          { port: 5173, owner: { pid: 4242, cmdline: "vite --port 5173", session: "dev" } },
        ],
      } as PreviewInfo,
    });
    render(
      withClient(
        <ExecutionCanvas events={[BUNDLER_HTML]} status="RUNNING" cid="conv_e2e3firefox" />,
      ),
    );
    // Find the link by its visible text. The exact URL is built from
    // `${agentHttpBase()}/conversations/${cid}/preview-app/` (cid URL-encoded).
    // We assert on the path route (not the base) so the test is robust to the
    // agent_http_base() pin above.
    const link = screen.getByRole("link", { name: /open in new tab/i });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("href")).toMatch(
      /\/conversations\/conv_e2e3firefox\/preview-app\/$/,
    );
  });
});

// ---- C5: FilesPane — suppress "0 B" for zero-byte (server-side) artifacts ----
//
// slides_generate / deliverable files land with content="" → bytes=0 in deriveFiles.
// Before the fix the pane showed "0 B" next to the path, which was misleading since
// the file is real and live on the server. After the fix, zero-byte entries show
// nothing (the span is suppressed) so the user sees only the path.

function slidesObservationEvent(filename: string): AgentEvent {
  return {
    kind: "observation",
    id: `obs-${filename}`,
    action_id: `act-${filename}`,
    tool_result: {
      tool_name: "slides_generate",
      success: true,
      content: "",
      structured: { filename },
    },
  } as AgentEvent;
}

describe("FilesPane — C5: zero-byte server-side artifacts do not render '0 B'", () => {
  it("does NOT render '0 B' for a server-side slides artifact (bytes=0)", () => {
    render(<FilesPane events={[slidesObservationEvent("deck.html")]} streamingFile={null} />);
    expect(screen.queryByText(/0 B/)).toBeNull();
  });

  it("DOES render the byte count for a client-side file_write artifact (bytes > 0)", () => {
    const events: AgentEvent[] = [
      {
        kind: "action",
        id: "act-w",
        thought: "",
        tool_call: {
          tool_name: "file_write",
          arguments: { path: "app.html", content: "<h1>hello</h1>" },
          call_id: "c-w",
        },
      } as AgentEvent,
    ];
    render(<FilesPane events={events} streamingFile={null} />);
    expect(screen.getByText(/\d+ B/)).toBeInTheDocument();
    expect(screen.queryByText(/^0 B$/)).toBeNull();
  });
});

// ---- C5: PreviewPane — inline artifact iframe for server-side HTML -----------
//
// When a server-side HTML artifact exists (slides_generate / deliverable with
// empty content), deriveSrcDoc returns null (C5 fix) and the pane should fall
// through to the ?inline=true iframe rather than the blank-frame or placeholder.
// The inline iframe must use sandbox="allow-scripts" WITHOUT allow-same-origin.
//
// We test PreviewPane directly (not wrapped in ExecutionCanvas) to avoid
// Radix Tabs lazy-rendering: inactive tab panels are not rendered to the DOM
// until first activation, which makes assertions on the iframe impossible
// without simulating a tab-click.  PreviewPane is the unit that owns the inline
// render logic, so direct testing is more precise anyway.

describe("PreviewPane — C5: server-side HTML artifact renders via ?inline=true iframe", () => {
  // The E2/E3 tests above call mockReturnValue; vi.clearAllMocks() (afterEach) clears
  // calls/results but NOT return values.  Reset to the default (no proxy) before each
  // C5 test so the mock doesn't bleed between suites.
  beforeEach(() => {
    useBuildPreviewMock.mockReturnValue({ data: null });
  });

  it("renders an 'Artifact preview' iframe (not a blank placeholder) for a server-side HTML artifact", () => {
    render(
      withClient(
        <PreviewPane
          events={[slidesObservationEvent("deck.html")]}
          status="FINISHED"
          cid="conv_c5inline"
        />,
      ),
    );
    // The inline artifact iframe should be present.
    const iframe = screen.getByTitle("Artifact preview");
    expect(iframe).toBeInTheDocument();
    // The src must point at the ?inline=true route.
    expect(iframe.getAttribute("src")).toMatch(/\/artifacts\/deck\.html\?inline=true/);
    // Must NOT have allow-same-origin — that would let the framed page reach APIs.
    expect(iframe.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(iframe.getAttribute("sandbox")).toContain("allow-scripts");
  });

  it("uses empty sandbox (no allow-scripts) for an untrusted run with inline artifact", () => {
    render(
      withClient(
        <PreviewPane
          events={[slidesObservationEvent("deck.html")]}
          status="FINISHED"
          cid="conv_c5trust"
          untrusted
        />,
      ),
    );
    // untrusted → sandbox="" (scripts disabled) so a compromised deck can't reach APIs.
    const iframe = screen.getByTitle("Artifact preview");
    expect(iframe).toBeInTheDocument();
    expect(iframe.getAttribute("sandbox")).toBe("");
  });

  it("does NOT render 'Artifact preview' when cid is null (no route to fetch from)", () => {
    render(
      withClient(
        <PreviewPane
          events={[slidesObservationEvent("deck.html")]}
          status="FINISHED"
          cid={null}
        />,
      ),
    );
    // cid is null → no inline URL can be constructed → falls back to placeholder.
    expect(screen.queryByTitle("Artifact preview")).not.toBeInTheDocument();
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

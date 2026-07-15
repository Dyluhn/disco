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
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode, type ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
const { useBuildPreviewMock, pathPreviewBootstrapUrlMock, previewBootstrapUrlMock } = vi.hoisted(() => ({
  useBuildPreviewMock: vi.fn<[{ data: PreviewInfo | null }]>(() => ({ data: null })),
  pathPreviewBootstrapUrlMock: vi.fn((cid: string) =>
    Promise.resolve({
      url: `http://localhost:8000/__disco/path-preview-auth/${cid}`,
      intent: `path-intent-${cid}`,
    }),
  ),
  previewBootstrapUrlMock: vi.fn((cid: string, port: number) =>
    Promise.resolve({
      url: `http://${cid}-${port}.localhost:8000/__disco/preview-auth`,
      intent: `host-intent-${cid}-${port}`,
    }),
  ),
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
    livePathPreviewBootstrapUrl: pathPreviewBootstrapUrlMock,
    pathPreviewBootstrapUrl: pathPreviewBootstrapUrlMock,
    previewBootstrapUrl: previewBootstrapUrlMock,
  };
});

describe("previewHostUrl (DC-01)", () => {
  it("returns null when base is unconfigured", () => {
    expect(previewHostUrl("conv_12345678", 8000, "")).toBeNull();
  });

  it("extracts cid8 and maps 127.0.0.1 to .localhost", () => {
    expect(previewHostUrl("conv_abcdef123456", 8000, "http://127.0.0.1:8000")).toBe("http://p2-abcdef12-8000.localhost:8000");
  });

  it("extracts cid8 and maps localhost to .localhost", () => {
    expect(previewHostUrl("conv_abcdef123456", 5173, "http://localhost:8000")).toBe("http://p2-abcdef12-5173.localhost:8000");
  });

  it("prepends cid8-port to other hostnames", () => {
    expect(previewHostUrl("conv_11112222", 8000, "http://my-magic-dns.net")).toBe("http://p2-11112222-8000.my-magic-dns.net");
    expect(previewHostUrl("conv_11112222", 8000, "https://my-magic-dns.net:443")).toBe("https://p2-11112222-8000.my-magic-dns.net");
  });

  it("maps IPv6 loopback and fails closed on bare remote IPs", () => {
    expect(previewHostUrl("conv_abcdef123456", 8000, "http://[::1]:8000")).toBe(
      "http://p2-abcdef12-8000.localhost:8000",
    );
    expect(previewHostUrl("conv_abcdef123456", 8000, "http://192.0.2.10:8000")).toBeNull();
    expect(previewHostUrl("conv_abcdef123456", 8000, "http://[2001:db8::1]:8000")).toBeNull();
  });

  it("drops a relative API prefix from the wildcard preview origin", () => {
    const expected = new URL("/", window.location.origin);
    expected.hostname = "p2-abcdef12-8000.localhost";
    expect(previewHostUrl("conv_abcdef123456", 8000, "/svc/agent")).toBe(
      expected.toString().replace(/\/+$/, ""),
    );
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

function appDeliverable(path: string): AgentEvent {
  return {
    id: `deliverable-${path}`,
    kind: "deliverable",
    title: "Selected app",
    path,
    artifact_kind: "app",
    deployment_url: "",
  } as AgentEvent;
}

const HTML = fileWrite("index.html", "<html><body><h1>Done</h1></body></html>");
const NOTE = fileWrite("notes.txt", "no html here");

function unresolvedCapability(): Promise<never> {
  return new Promise(() => {});
}

function enableLiveCapability() {
  previewBootstrapUrlMock.mockImplementation((cid: string, port: number) =>
    Promise.resolve({
      url: `http://${cid}-${port}.localhost:8000/__disco/preview-auth`,
      intent: `host-intent-${cid}-${port}`,
    }),
  );
}

beforeEach(() => {
  useBuildPreviewMock.mockReturnValue({ data: null });
  // PreviewPane mints a live capability for every cid, even when the rendered
  // branch is static. Most tests do not exercise that effect; leaving its explicit
  // mock pending prevents an irrelevant state update after a synchronous test ends.
  previewBootstrapUrlMock.mockImplementation(unresolvedCapability);
});

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

describe("PreviewPane — selected handoff entry", () => {
  it("renders the selected nested app and never the stale workspace root", () => {
    useBuildPreviewMock.mockReturnValue({ data: null });
    render(
      withClient(
        <PreviewPane
          events={[
            fileWrite("index.html", "<h1>STALE ROOT MUST NEVER OPEN</h1>"),
            fileWrite("release/index.html", "<h1>SELECTED RELEASE ONE</h1>"),
            appDeliverable("release/index.html"),
          ]}
          status="FINISHED"
          cid="conv_selected_entry"
        />,
      ),
    );

    const srcdoc = screen.getByTitle("Static preview").getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain("SELECTED RELEASE ONE");
    expect(srcdoc).not.toContain("STALE ROOT MUST NEVER OPEN");
    expect(srcdoc).not.toContain("<base ");
    expect(srcdoc).not.toContain("/preview-app/");
  });

  it("loads a finished trusted multi-file site from the isolated static origin", async () => {
    useBuildPreviewMock.mockReturnValue({ data: null });
    render(
      withClient(
        <PreviewPane
          events={[
            fileWrite(
              "release/index.html",
              '<link rel="stylesheet" href="assets/site.css"><script src="assets/site.js"></script>',
            ),
            fileWrite("release/assets/site.css", "body{color:green}"),
            fileWrite("release/assets/site.js", "document.body.dataset.scriptLoaded='true'"),
            appDeliverable("release/index.html"),
            {
              id: "version-1",
              kind: "workspace_version",
              version_seq: 1,
              tree_digest: "selected-tree",
              trigger: "finish",
            } as AgentEvent,
          ]}
          status="FINISHED"
          cid="conv_a1b2c3d4selected"
        />,
      ),
    );

    await waitFor(() => {
      const frame = screen.getByTitle("Static preview");
      expect(frame).not.toHaveAttribute("src");
      expect(frame).not.toHaveAttribute("srcdoc");
      expect(frame).toHaveAttribute("sandbox", "allow-scripts allow-same-origin");
    });
    expect(pathPreviewBootstrapUrlMock).toHaveBeenCalledWith(
      "conv_a1b2c3d4selected",
      "/",
    );
  });

  it("loads the committed static origin when app bytes exist only in the workspace", async () => {
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: true,
        owner: { pid: 6161, cmdline: "python -m http.server 8000", session: "preview" },
        ports: [
          {
            port: 8000,
            owner: { pid: 6161, cmdline: "python -m http.server 8000", session: "preview" },
          },
        ],
      } as PreviewInfo,
    });
    render(
      withClient(
        <PreviewPane
          events={[
            // Shell-created files do not carry their bytes in the event stream.
            // The deliverable and committed version are sufficient to prove the
            // selected snapshot exists and must outrank any live-server fallback.
            appDeliverable("release/index.html"),
            {
              id: "version-shell-created",
              kind: "workspace_version",
              version_seq: 1,
              tree_digest: "shell-created-tree",
              trigger: "finish",
            } as AgentEvent,
          ]}
          status="FINISHED"
          cid="conv_a1b2c3d4shell"
        />,
      ),
    );

    await waitFor(() => {
      expect(screen.getByTitle("Static preview")).not.toHaveAttribute("src");
    });
    expect(screen.queryByTitle("Live preview")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Artifact preview")).not.toBeInTheDocument();
    expect(pathPreviewBootstrapUrlMock).toHaveBeenCalledWith("conv_a1b2c3d4shell", "/");
  });

  it("does not mint unused live capabilities while the committed preview is authoritative", async () => {
    previewBootstrapUrlMock.mockClear();
    pathPreviewBootstrapUrlMock.mockClear();
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: true,
        owner: { pid: 6161, cmdline: "python -m http.server 8000", session: "preview" },
        ports: [
          {
            port: 8000,
            owner: { pid: 6161, cmdline: "python -m http.server 8000", session: "preview" },
          },
        ],
      } as PreviewInfo,
    });
    render(
      withClient(
        <StrictMode>
          <PreviewPane
            events={[
              appDeliverable("release/index.html"),
              {
                id: "version-authoritative",
                kind: "workspace_version",
                version_seq: 1,
                tree_digest: "authoritative-tree",
                trigger: "finish",
              } as AgentEvent,
            ]}
            status="FINISHED"
            cid="conv_a1b2c3d4authoritative"
          />
        </StrictMode>,
      ),
    );

    await waitFor(() =>
      expect(pathPreviewBootstrapUrlMock).toHaveBeenCalledWith(
        "conv_a1b2c3d4authoritative",
        "/",
      ),
    );
    expect(previewBootstrapUrlMock).not.toHaveBeenCalled();
    expect(pathPreviewBootstrapUrlMock).toHaveBeenCalledTimes(1);
  });

  it("remints the committed launch after switching live and back", async () => {
    enableLiveCapability();
    pathPreviewBootstrapUrlMock.mockClear();
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: true,
        owner: { pid: 6161, cmdline: "python -m http.server 8000", session: "preview" },
        ports: [
          {
            port: 8000,
            owner: { pid: 6161, cmdline: "python -m http.server 8000", session: "preview" },
          },
        ],
      } as PreviewInfo,
    });
    render(
      withClient(
        <PreviewPane
          events={[
            HTML,
            appDeliverable("index.html"),
            {
              id: "version-remount",
              kind: "workspace_version",
              version_seq: 1,
              tree_digest: "remount-tree",
              trigger: "finish",
            } as AgentEvent,
          ]}
          status="FINISHED"
          cid="conv_remount"
        />,
      ),
    );
    await screen.findByTitle("Static preview");
    expect(pathPreviewBootstrapUrlMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /live server/i }));
    await screen.findByTitle("Live preview");
    fireEvent.click(screen.getByRole("button", { name: /rendered/i }));
    await screen.findByTitle("Static preview");
    await waitFor(() => expect(pathPreviewBootstrapUrlMock).toHaveBeenCalledTimes(2));
  });

  it("never gives an untrusted imported site the isolated executable origin", () => {
    pathPreviewBootstrapUrlMock.mockClear();
    render(
      withClient(
        <PreviewPane
          events={[
            fileWrite("release/index.html", '<script src="assets/site.js"></script>'),
            fileWrite("release/assets/site.js", "globalThis.pwned=true"),
            appDeliverable("release/index.html"),
          ]}
          status="FINISHED"
          cid={null}
          untrusted
        />,
      ),
    );
    const frame = screen.getByTitle("Static preview");
    expect(frame).toHaveAttribute("srcdoc");
    expect(frame).toHaveAttribute("sandbox", "");
    expect(pathPreviewBootstrapUrlMock).not.toHaveBeenCalled();
  });
});

// ---- E2 — bundler entry + live proxy → default to the live server, not srcdoc
//
// The srcDoc iframe cannot resolve `/src/main.tsx` or `/@vite/client` (those
// are dev-server virtual modules). Showing the user a broken srcdoc when the
// dev server is up is a false affordance — they think the build is broken when
// it's actually fine. Default to the live server for bundler entries.

describe("PreviewPane — E2: bundler entry defaults to live server when proxy is available", () => {
  it("shows the live iframe (not the broken srcdoc) by default for a Vite entry", async () => {
    enableLiveCapability();
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
    expect(await screen.findByTitle("Live preview")).toBeInTheDocument();
    expect(screen.queryByTitle("Static preview")).not.toBeInTheDocument();
    // The header is the live-server header, not the preview header.
    expect(screen.getByText("live server")).toBeInTheDocument();
    await screen.findByRole("button", { name: /open in new tab/i });
  });

  it("defaults to RENDERED for plain static HTML even when a proxy is available (2026-07-07 walkthrough)", async () => {
    enableLiveCapability();
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
    const liveButton = screen.getByRole("button", { name: /live/i });
    expect(liveButton).toBeInTheDocument();
    fireEvent.click(liveButton);
    await screen.findByRole("button", { name: /open in new tab/i });
  });

  it("remints the live launch after toggling rendered and back", async () => {
    enableLiveCapability();
    previewBootstrapUrlMock.mockClear();
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: true,
        owner: { pid: 5151, cmdline: "python -m http.server 8000", session: "preview" },
        ports: [
          { port: 8000, owner: { pid: 5151, cmdline: "python -m http.server 8000", session: "preview" } },
        ],
      } as PreviewInfo,
    });
    render(withClient(<PreviewPane events={[HTML]} status="RUNNING" cid="conv_live_remount" />));

    fireEvent.click(screen.getByRole("button", { name: /live server/i }));
    await screen.findByTitle("Live preview");
    expect(previewBootstrapUrlMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /rendered/i }));
    await screen.findByTitle("Static preview");
    fireEvent.click(screen.getByRole("button", { name: /live server/i }));
    await screen.findByTitle("Live preview");
    await waitFor(() => expect(previewBootstrapUrlMock).toHaveBeenCalledTimes(2));
  });
});

// ---- E3 — Firefox-safe Open-in-new-tab link via isolated path capability ----
//
// The origin-true `{cid8}-{port}.localhost` URL is the most correct, but
// Firefox won't resolve `.localhost` subdomains out of the box. The
// agent-server can bootstrap its path route on an alternate isolated origin.
// Surface that capability URL so Firefox / Safari / non-magic-DNS users have
// an escape hatch without executing generated code on the authenticated app
// origin (do not remove the iframe — Chrome users get the correct asset origin).

describe("PreviewPane — E3: isolated cross-browser live preview link", () => {
  it("mints a path capability instead of opening generated code on the authenticated agent origin", async () => {
    enableLiveCapability();
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
    const popup = { opener: window, close: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    const submissions: Array<{ action: string; target: string }> = [];
    vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(function () {
      submissions.push({ action: this.action, target: this.target });
    });
    const button = await screen.findByRole("button", { name: /open in new tab/i });
    expect(pathPreviewBootstrapUrlMock).not.toHaveBeenCalled();
    fireEvent.click(button);
    await waitFor(() =>
      expect(pathPreviewBootstrapUrlMock).toHaveBeenCalledWith(
        "conv_e2e3firefox",
        expect.stringMatching(/^\/\?r=\d+$/),
      ),
    );
    await waitFor(() =>
      expect(submissions).toContainEqual({
        action: "http://localhost:8000/__disco/path-preview-auth/conv_e2e3firefox",
        target: expect.stringMatching(/^disco-preview-popup-/),
      }),
    );
    expect(open).toHaveBeenCalledWith("about:blank", expect.stringMatching(/^disco-preview-popup-/));
    expect(popup.opener).toBeNull();
  });

  it("fails closed when the isolated path capability cannot be created", async () => {
    enableLiveCapability();
    pathPreviewBootstrapUrlMock.mockRejectedValueOnce(new Error("capability unavailable"));
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: true,
        owner: { pid: 4242, cmdline: "vite --port 8000", session: "dev" },
        ports: [
          { port: 8000, owner: { pid: 4242, cmdline: "vite --port 8000", session: "dev" } },
        ],
      } as PreviewInfo,
    });
    render(
      withClient(
        <ExecutionCanvas events={[BUNDLER_HTML]} status="RUNNING" cid="conv_e2e3failclosed" />,
      ),
    );
    const popup = { opener: window, close: vi.fn() };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    fireEvent.click(await screen.findByRole("button", { name: /open in new tab/i }));
    await waitFor(() => expect(pathPreviewBootstrapUrlMock).toHaveBeenCalled());
    await waitFor(() => expect(popup.close).toHaveBeenCalled());
    expect(document.body.innerHTML).not.toContain(
      "/conversations/conv_e2e3failclosed/preview-app/",
    );
  });

  it("never loads a raw wildcard URL when live capability minting fails", async () => {
    previewBootstrapUrlMock.mockRejectedValueOnce(new Error("live capability unavailable"));
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: true,
        owner: { pid: 4242, cmdline: "vite --port 8000", session: "dev" },
        ports: [
          { port: 8000, owner: { pid: 4242, cmdline: "vite --port 8000", session: "dev" } },
        ],
      } as PreviewInfo,
    });
    render(
      withClient(
        <ExecutionCanvas events={[BUNDLER_HTML]} status="RUNNING" cid="conv_e2e3livefail" />,
      ),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Live preview unavailable: isolated preview access could not be established.",
    );
    expect(screen.queryByTitle("Live preview")).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain("e2e3live-8000.localhost");
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

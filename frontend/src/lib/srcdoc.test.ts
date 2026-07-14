/**
 * Cluster 5 — the client-side srcdoc preview selector (UI 2.1). Renders the
 * written files into a self-contained HTML document with no backend.
 */

import { describe, expect, it } from "vitest";
import { deriveFiles, deriveSrcDoc } from "@/lib/buildTrace";
import type { AgentEvent, WorkspaceFile } from "@/lib/buildTrace";

function f(path: string, content: string): WorkspaceFile {
  return { path, content, bytes: content.length };
}

function fileAction(id: string, tool: string, args: Record<string, unknown>): AgentEvent {
  return {
    kind: "action",
    id,
    thought: "",
    tool_call: { tool_name: tool, arguments: args, call_id: id },
  } as AgentEvent;
}

describe("deriveFiles — reconstructs workspace files from the trace", () => {
  it("captures file_write content", () => {
    const files = deriveFiles([fileAction("1", "file_write", { path: "index.html", content: "<h1>hi</h1>" })]);
    expect(files).toEqual([{ path: "index.html", content: "<h1>hi</h1>", bytes: 11 }]);
  });

  it("accumulates file_append so an incrementally-written file renders", () => {
    const files = deriveFiles([
      fileAction("1", "file_write", { path: "a.txt", content: "one\n" }),
      fileAction("2", "file_append", { path: "a.txt", content: "two\n" }),
      fileAction("3", "file_append", { path: "b.txt", content: "fresh" }), // append to unseen file
    ]);
    const byPath = Object.fromEntries(files.map((x) => [x.path, x.content]));
    expect(byPath["a.txt"]).toBe("one\ntwo\n");
    expect(byPath["b.txt"]).toBe("fresh");
  });

  it("mirrors a file_edit so the preview tracks it", () => {
    const files = deriveFiles([
      fileAction("1", "file_write", { path: "i.html", content: "<title>OLD</title>" }),
      fileAction("2", "file_edit", { path: "i.html", old: "OLD", new: "NEW" }),
    ]);
    expect(files[0].content).toBe("<title>NEW</title>");
  });

  it("falls back to manifest files when the trace has no file writes", () => {
    const files = deriveFiles([], [{ path: "imported/index.html", bytes: 42 }]);
    expect(files).toEqual([{ path: "imported/index.html", content: "", bytes: 42 }]);
  });

  it("prefers event-derived files over manifest fallback", () => {
    const files = deriveFiles(
      [fileAction("1", "file_write", { path: "index.html", content: "<h1>live</h1>" })],
      [{ path: "stale.html", bytes: 12 }],
    );
    expect(files).toEqual([{ path: "index.html", content: "<h1>live</h1>", bytes: 13 }]);
  });
});

describe("deriveSrcDoc", () => {
  it("returns null when there is no HTML artifact", () => {
    expect(deriveSrcDoc([])).toBeNull();
    expect(deriveSrcDoc([f("app.js", "console.log(1)")])).toBeNull();
  });

  // C5: server-side artifacts (slides_generate / deliverable) land with
  // content="" — deriveSrcDoc must return null so the `srcDoc != null` guards
  // suppress the blank white iframe.  Before this fix it returned "", which the
  // guard misread as a real document and rendered an empty frame.
  it("returns null for a content-empty .html (server-side artifact, C5)", () => {
    expect(deriveSrcDoc([f("deck.html", "")])).toBeNull();
    expect(deriveSrcDoc([f("index.html", "")])).toBeNull();
  });

  it("returns non-null for a real-content .html (not a server-side artifact)", () => {
    const doc = deriveSrcDoc([f("deck.html", "<section>slide</section>")]);
    expect(doc).toBe("<section>slide</section>");
  });

  it("renders a bare index.html as-is", () => {
    const doc = deriveSrcDoc([f("index.html", "<h1>Hi</h1>")]);
    expect(doc).toBe("<h1>Hi</h1>");
  });

  it("renders the selected handoff entry instead of an earlier stale root", () => {
    const doc = deriveSrcDoc(
      [
        f("index.html", "<h1>STALE ROOT</h1>"),
        f("release/index.html", "<h1>SELECTED RELEASE</h1>"),
      ],
      undefined,
      "http://agent.test/conversations/conv_selected/preview-app/",
      "release/index.html",
    );

    expect(doc).toContain("SELECTED RELEASE");
    expect(doc).not.toContain("STALE ROOT");
  });

  it("fails closed when the selected handoff entry is absent", () => {
    expect(
      deriveSrcDoc(
        [f("index.html", "<h1>STALE ROOT</h1>")],
        undefined,
        undefined,
        "release/index.html",
      ),
    ).toBeNull();
  });

  it("inlines a local stylesheet link", () => {
    const doc = deriveSrcDoc([
      f("index.html", '<head><link rel="stylesheet" href="style.css"></head><body>x</body>'),
      f("style.css", "body { color: red; }"),
    ]);
    expect(doc).toContain("<style>");
    expect(doc).toContain("body { color: red; }");
    expect(doc).not.toContain('<link rel="stylesheet"');
  });

  it("inlines a local script src", () => {
    const doc = deriveSrcDoc([
      f("index.html", '<body><script src="app.js"></script></body>'),
      f("app.js", "alert('hi')"),
    ]);
    expect(doc).toContain("<script>");
    expect(doc).toContain("alert('hi')");
    expect(doc).not.toContain('src="app.js"');
  });

  it("resolves ./relative and subdir paths by basename", () => {
    const doc = deriveSrcDoc([
      f("index.html", '<link rel="stylesheet" href="./css/main.css">'),
      f("css/main.css", ".x{}"),
    ]);
    expect(doc).toContain(".x{}");
  });

  it("leaves a remote (CDN) reference untouched", () => {
    const doc = deriveSrcDoc([
      f("index.html", '<script src="https://cdn.example.com/lib.js"></script>'),
    ]);
    expect(doc).toContain('src="https://cdn.example.com/lib.js"');
  });

  it("adds a preview base URL for every non-inlined relative asset class", () => {
    const base = "http://agent.test/conversations/conv_multi/preview-app/";
    const doc = deriveSrcDoc(
      [
        f(
          "release/index.html",
          '<html><head></head><body><img src="media/hero image.svg"><a href="docs/?mode=full#part">Docs</a></body></html>',
        ),
      ],
      undefined,
      base,
    );
    expect(doc).toContain(`<base href="${base}">`);
    expect(new URL("media/hero image.svg", base).pathname).toBe(
      "/conversations/conv_multi/preview-app/media/hero%20image.svg",
    );
    expect(new URL("docs/?mode=full#part", base).href).toBe(
      `${base}docs/?mode=full#part`,
    );
  });

  it("keeps CSS and JavaScript external when a real base route is available", () => {
    const base = "http://agent.test/conversations/conv_multi/preview-app/";
    const doc = deriveSrcDoc(
      [
        f(
          "release/index.html",
          '<link rel="stylesheet" href="assets/theme.css"><script src="scripts/app.js"></script>',
        ),
        f("release/assets/theme.css", '@font-face{src:url("../fonts/app.woff2")}'),
        f("release/scripts/app.js", "globalThis.loaded = true"),
      ],
      undefined,
      base,
    );
    expect(doc).toContain('href="assets/theme.css"');
    expect(doc).toContain('src="scripts/app.js"');
    expect(doc).not.toContain("<style>");
    expect(new URL("../fonts/app.woff2", new URL("assets/theme.css", base)).pathname).toBe(
      "/conversations/conv_multi/preview-app/fonts/app.woff2",
    );
  });

  it("escapes a preview base URL before inserting it into HTML", () => {
    const doc = deriveSrcDoc(
      [f("index.html", "<h1>safe</h1>")],
      undefined,
      'https://agent.test/preview/?a=1&b="unsafe"',
    );
    expect(doc).toContain(
      '<base href="https://agent.test/preview/?a=1&amp;b=&quot;unsafe&quot;">',
    );
  });

  it("falls back to any *.html when there's no index.html", () => {
    const doc = deriveSrcDoc([f("about.html", "<p>about</p>")]);
    expect(doc).toBe("<p>about</p>");
  });
});

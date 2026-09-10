import { describe, expect, it } from "vitest";

import {
  type DiscoResolvedTarget,
  resolveSemanticTarget,
  semanticEditTarget,
} from "@/lib/resolvers/discoSemanticResolver";

/** Build a detached DOM tree from HTML and return the element matching `sel` (the click). */
function clickTarget(html: string, sel: string): Element {
  const root = document.createElement("div");
  root.innerHTML = html;
  const el = root.querySelector(sel);
  if (!el) throw new Error(`no element for ${sel}`);
  return el;
}

function resolve(html: string, sel: string): DiscoResolvedTarget | null {
  return resolveSemanticTarget(clickTarget(html, sel));
}

describe("discoSemanticResolver", () => {
  it("click hero headline → field hero.headline", () => {
    const t = resolve(
      '<section data-disco-section="hero"><h1 data-disco-field="headline">Hi</h1></section>',
      "h1",
    );
    expect(t).toEqual({ kind: "field", section: "hero", field: "headline" });
    expect(semanticEditTarget(t as never)).toBe("hero.headline");
  });

  it("click plain text inside a section → the section target", () => {
    const t = resolve('<section data-disco-section="hero"><div>plain</div></section>', "div");
    expect(t).toEqual({ kind: "section", section: "hero" });
  });

  it("indexed item beats the enclosing section (closest-first)", () => {
    const t = resolve(
      '<section data-disco-section="services">' +
        '<div data-disco-collection="services.cards" data-disco-index="1" data-disco-item-kind="card">c</div>' +
        "</section>",
      "div",
    );
    expect(t).toEqual({ kind: "indexed", collection: "services.cards", index: 1, itemKind: "card" });
    expect(semanticEditTarget(t as never)).toBe("services.cards[1]");
  });

  it("comment anchor nested in a section wins over the section (closest-first)", () => {
    const t = resolve(
      '<section data-disco-section="hero"><span data-disco-comment-anchor="note"><b>x</b></span></section>',
      "b",
    );
    expect(t).toEqual({ kind: "comment_anchor", anchor: "note" });
  });

  it("field WITHOUT an enclosing section fails closed → source fallback", () => {
    const t = resolve('<div data-disco-field="headline" data-oid="index.html:5">x</div>', "div");
    expect(t).toEqual({ kind: "source", oid: "index.html:5", file: "index.html", line: 5 });
  });

  it("field without section AND without oid → null (never a section-less field)", () => {
    const t = resolve('<div data-disco-field="headline">x</div>', "div");
    expect(t).toBeNull();
  });

  it("invalid (negative) index → not resolved as indexed → source fallback", () => {
    const t = resolve(
      '<div data-disco-collection="x.y" data-disco-index="-1" data-oid="a.html:3">c</div>',
      "div",
    );
    expect(t).toEqual({ kind: "source", oid: "a.html:3", file: "a.html", line: 3 });
  });

  it("data-oid source fallback when no data-disco metadata is present", () => {
    const t = resolve('<div data-oid="index.html:12">x</div>', "div");
    expect(t).toEqual({ kind: "source", oid: "index.html:12", file: "index.html", line: 12 });
  });

  it("unknown DOM with no data-disco and no data-oid → null (no false affordance)", () => {
    const t = resolve("<div><span>nothing</span></div>", "span");
    expect(t).toBeNull();
  });

  it("file metadata resolves when nearer than nothing else", () => {
    const t = resolve('<div data-disco-file="index.html"><p>x</p></div>', "p");
    expect(t).toEqual({ kind: "file", file: "index.html" });
  });
});

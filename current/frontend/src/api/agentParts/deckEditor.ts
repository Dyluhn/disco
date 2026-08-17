/**
 * A2 — the in-app deck editor API: fetch the LoweredDeck / rendered HTML for the
 * WYSIWYG editor iframe substrate, and apply RFC-6902 patches back to the server.
 *
 * Extracted from api/agent.ts (module-size decomposition, PKG-12-FE-BUILD/
 * TS-0002); re-exported from api/agent so the public import path is unchanged.
 */

import type { JsonPatchOp, LoweredDeck } from "@/components/build/editor/types";
import type { DeckPatchResult } from "../agent";
import { agentFetch, agentGet, agentHttpBase, agentLive, agentSend } from "../client";

/** A small fixture LoweredDeck so the editor pane renders offline (development/tests/screenshots). */
const _FIXTURE_LOWERED_DECK: LoweredDeck = {
  title: "Sample Deck",
  theme_name: "disco",
  theme_mode: "light",
  slides: [
    {
      slide_id: "slide-0",
      slide_idx: 0,
      layout: "title",
      bg_color: "#ffffff",
      elements: [
        {
          element_id: "slide-0:title",
          slide_id: "slide-0",
          kind: "title",
          content: "Sample Title",
          geometry: { x: 4, y: 7, w: 92, h: 13 },
          font_size_vw: 2.5,
          font_weight: "bold",
          font_style: "normal",
          json_pointer: "/slides/0/title",
        },
      ],
    },
  ],
};

/** A2.1: fetch the LoweredDeck for an editable deck (its `{base}.authored.json`).
 * Offline → the fixture deck so the editor renders in development/tests/screenshots. */
export async function getDeckForEditor(cid: string, base: string): Promise<LoweredDeck> {
  if (!agentLive()) return _FIXTURE_LOWERED_DECK;
  return agentGet<LoweredDeck>(
    `/conversations/${encodeURIComponent(cid)}/deck/editor?path=${encodeURIComponent(base)}`,
  );
}


/** A2.2: fetch the inline HTML render of a deck for the WYSIWYG editor iframe substrate.
 *
 * The render includes `data-element-id` + `data-slide-id` stamps on editable elements,
 * which the editor overlay system uses to measure positions and wire click-to-edit.
 * Offline → a minimal slide HTML so the editor shows something in development/tests/screenshots. */
export async function getDeckRenderHtml(
  cid: string,
  base: string,
  template?: string,
): Promise<string> {
  if (!agentLive()) {
    // Minimal fixture: one active slide with a stamped title element.
    return (
      `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">` +
      `<style>.slide{display:none}.slide.active{display:flex}</style></head>` +
      `<body><div class="deck">` +
      `<section class="slide active" data-slide-id="slide-0" id="slide-0">` +
      `<h2 data-element-id="slide-0:title" data-slide-id="slide-0">Sample Title</h2>` +
      `</section></div></body></html>`
    );
  }
  const params = new URLSearchParams({ path: base });
  if (template) params.set("template", template);
  const url = `${agentHttpBase()}/conversations/${encodeURIComponent(cid)}/deck/editor/render?${params}`;
  const res = await agentFetch(url, { headers: { accept: "text/html" } });
  if (!res.ok) throw new Error(`Deck render fetch failed: ${res.status}`);
  return res.text();
}

/** A2.3: apply an RFC-6902 patch to the deck, re-render, and return the new
 * server-authoritative LoweredDeck. Throws ApiError on failure — notably 409
 * (no live sandbox: the build's workspace is suspended) and 422 (rejected patch);
 * the caller surfaces those distinctly. Offline → echo the fixture deck. */
export async function patchDeck(
  cid: string,
  base: string,
  patch: JsonPatchOp[],
): Promise<DeckPatchResult> {
  if (!agentLive())
    return {
      ok: true,
      lowered: _FIXTURE_LOWERED_DECK,
      html_file: `${base}.html`,
      pptx_file: `${base}.pptx`,
    };
  return agentSend<DeckPatchResult>(
    "PUT",
    `/conversations/${encodeURIComponent(cid)}/deck/editor?path=${encodeURIComponent(base)}`,
    { patch },
  );
}

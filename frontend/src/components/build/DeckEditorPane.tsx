/**
 * DeckEditorPane — §5d host for the WYSIWYG in-app slide editor.
 *
 * Fetches two things concurrently:
 *  1. The `LoweredDeck` (GET /deck/editor) — the edit map: element_id → json_pointer +
 *     current content. This is the SERVER'S source of truth for the authoring schema.
 *  2. The inline render HTML (GET /deck/editor/render) — injected as the iframe srcDoc
 *     that shows the REAL rendered deck (brand fonts, chrome, exact layout).
 *
 * After each PUT patch the deck AND the render HTML are both re-fetched so the iframe
 * reflects the edit. The server is the source of truth: the returned `lowered` replaces
 * local state; a rejected patch never desyncs the UI.
 *
 * No false affordances:
 *  - 409 (sandbox suspended) → "re-open to edit" notice, not a silent failure.
 *  - 422 (rejected patch) → reason surfaced, prior server state kept.
 *  - renderHtml load failure → graceful degradation (overlays at zero position, no crash).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { isApiFailure } from "@/api/errors";
import { getDeckForEditor, getDeckRenderHtml, patchDeck } from "@/api/agent";
import { DeckEditor } from "./editor/DeckEditor";
import { DeckExportBar } from "./DeckExportBar";
import type { JsonPatchOp, LoweredDeck } from "./editor/types";

interface DeckEditorPaneProps {
  /** The conversation id whose deck is being edited. */
  cid: string;
  /** The deck base name (no extension) — the slides tool's `base_name`. */
  base: string;
}

export function DeckEditorPane({ cid, base }: DeckEditorPaneProps) {
  const [deck, setDeck] = useState<LoweredDeck | null>(null);
  const [renderHtml, setRenderHtml] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [patchNotice, setPatchNotice] = useState<string | null>(null);
  const savingRef = useRef(false);
  // Monotonic token for render-HTML refreshes: a later refresh must win even if an
  // earlier fetch resolves after it, so the iframe never shows a stale render.
  const renderSeqRef = useRef(0);
  const [saving, setSaving] = useState(false);
  // BW-14: the export Theme is lifted here so a theme change also re-renders the LIVE
  // preview iframe (not just the export download links). Passed down to DeckExportBar
  // (controlled) and threaded into the render fetch below.
  const [templateId, setTemplateId] = useState("disco-light");

  // Load the load-critical editor data (the LoweredDeck) on mount / cid+base change.
  // The render HTML is fetched separately (below) so a theme switch can re-render the
  // preview without reloading the whole editor.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);

    getDeckForEditor(cid, base)
      .then((d) => {
        if (!cancelled) setDeck(d);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setLoadError(
          e instanceof Error ? e.message : "Failed to load the deck for editing.",
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [cid, base]);

  // BW-14: fetch the render HTML for the iframe substrate — re-running on a TEMPLATE
  // change so switching the Theme visibly re-renders the slides (not just the export
  // hrefs). Guarded with renderSeqRef so an earlier fetch can't clobber a newer one.
  // The render HTML is NON-fatal: a failure degrades to overlays at (0,0), not a crash.
  useEffect(() => {
    const seq = ++renderSeqRef.current;
    getDeckRenderHtml(cid, base, templateId)
      .then((html) => {
        if (seq === renderSeqRef.current) setRenderHtml(html);
      })
      .catch(() => {
        if (seq !== renderSeqRef.current) return;
        setRenderHtml(null);
        setPatchNotice(
          "Couldn't load the slide preview. Editing still saves; reopen the deck to restore the visual.",
        );
      });
  }, [cid, base, templateId]);

  const handlePatch = useCallback(
    (patch: JsonPatchOp[]) => {
      if (savingRef.current) return; // a save is in flight — drop, don't race/overwrite
      savingRef.current = true;
      setSaving(true);
      setPatchNotice(null);
      patchDeck(cid, base, patch)
        .then((res) => {
          // Server is the source of truth — reflect the returned lowered deck.
          setDeck(res.lowered);
          // Re-fetch the render HTML so the iframe shows the updated slide content.
          // Guard with a monotonic token so a slower earlier refresh can't overwrite
          // a newer one (rapid successive edits).
          const seq = ++renderSeqRef.current;
          getDeckRenderHtml(cid, base, templateId)
            .then((html) => {
              if (seq === renderSeqRef.current) setRenderHtml(html);
            })
            .catch(() => {
              /* render refresh failure is non-fatal — UI keeps the stale visual */
            });
          setPatchNotice(
            res.pdf_stale
              ? "Slides saved. The PDF download is now out of date — re-export the deck to refresh it."
              : null,
          );
        })
        .catch((e: unknown) => {
          if (isApiFailure(e) && e.status === 409) {
            setPatchNotice(
              "Re-open this build to edit slides (its workspace is suspended).",
            );
          } else if (isApiFailure(e) && e.status === 422) {
            setPatchNotice("That edit was rejected (it produced an invalid deck).");
          } else {
            setPatchNotice(
              e instanceof Error ? e.message : "Failed to apply the edit.",
            );
          }
        })
        .finally(() => {
          savingRef.current = false;
          setSaving(false);
        });
    },
    [cid, base, templateId],
  );

  if (loading)
    return (
      <div className="flex h-full items-center justify-center font-ui text-[0.82rem] text-text-faint">
        Loading deck…
      </div>
    );

  if (loadError || !deck)
    return (
      <div className="flex h-full flex-col items-center justify-center gap-hair px-body text-center font-ui text-[0.82rem] text-text-faint">
        <p className="text-warn">{loadError ?? "No editable deck found."}</p>
      </div>
    );

  return (
    <div
      className="flex h-full min-h-0 flex-col"
      // #12: the deck editor's real backend action is the autosave PUT /deck/editor
      // patch on each text commit (there is no separate Save button). Expose the
      // host + a deterministic save phase so a harness can address the editor and
      // await a save settling without racing the in-flight request.
      data-disco-control="build.deck-editor"
      data-deck-saving={saving ? "saving" : "idle"}
    >
      {/* W-19: export bar — re-export the deck being edited (Theme + pptx/html) without
          leaving the tab. Shares DeckExportBar with ActivityFeed's SlidesDownload (W-16). */}
      <div className="shrink-0 border-b border-hairline px-body py-hair">
        <DeckExportBar
          conversationId={cid}
          base={base}
          templateId={templateId}
          onTemplateChange={setTemplateId}
        />
      </div>
      {patchNotice && (
        <p
          role="alert"
          className="shrink-0 border-b border-hairline bg-warn/10 px-body py-hair font-ui text-[0.78rem] text-warn"
        >
          {patchNotice}
        </p>
      )}
      <div className="relative min-h-0 flex-1">
        <DeckEditor
          deck={deck}
          renderHtml={renderHtml}
          disableDrag
          onPatch={handlePatch}
        />
        {saving && (
          <div className="absolute inset-0 z-10 flex items-start justify-end bg-bg/30 p-inline">
            <span
              role="status"
              className="rounded-control border border-hairline bg-bg px-inline py-hair font-ui text-[0.74rem] text-text-muted"
            >
              Saving…
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

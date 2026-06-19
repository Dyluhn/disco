/**
 * DeckEditorPane — A2 host for the in-app slide TEXT editor.
 *
 * Fetches the deck's LoweredDeck (geometry computed server-side from the authored
 * schema), renders the `DeckEditor` in text-edit-only mode (`disableDrag` — the
 * AuthoredDeck schema has no element geometry), and round-trips each edit through
 * the PUT patch route. The SERVER is the source of truth: after a patch we replace
 * local state with the returned `lowered`, so a rejected patch never desyncs the UI.
 *
 * No false affordances:
 *  - a 409 (the build's sandbox is suspended) shows a clear "re-open to edit" notice,
 *    not a silent failure;
 *  - a 422 (rejected patch) surfaces the reason and keeps the prior server state.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api/client";
import { getDeckForEditor, patchDeck } from "@/api/agent";
import { DeckEditor } from "./editor/DeckEditor";
import type { JsonPatchOp, LoweredDeck } from "./editor/types";

interface DeckEditorPaneProps {
  /** The conversation id whose deck is being edited. */
  cid: string;
  /** The deck base name (no extension) — the slides tool's `base_name`. */
  base: string;
}

export function DeckEditorPane({ cid, base }: DeckEditorPaneProps) {
  const [deck, setDeck] = useState<LoweredDeck | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  // A non-load error surfaced after a patch (409 suspended / 422 rejected / other).
  const [patchNotice, setPatchNotice] = useState<string | null>(null);
  // Serialize saves: the PUT reads→patches→re-renders→rewrites with no server-side
  // lock, so a second edit fired before the first returns would read stale source and
  // overwrite it (lost update). We block new edits while a save is in flight.
  const savingRef = useRef(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    getDeckForEditor(cid, base)
      .then((d) => {
        if (!cancelled) setDeck(d);
      })
      .catch((e: unknown) => {
        if (!cancelled)
          setLoadError(e instanceof Error ? e.message : "Failed to load the deck for editing.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [cid, base]);

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
          // The HTML + PPTX are re-rendered on every patch, but a previously
          // exported PDF is NOT (the route can't construct a render ToolContext).
          // Warn so the user never downloads a stale PDF thinking it's current.
          setPatchNotice(
            res.pdf_stale
              ? "Slides saved. The PDF download is now out of date — re-export the deck to refresh it."
              : null,
          );
        })
        .catch((e: unknown) => {
          if (e instanceof ApiError && e.status === 409) {
            setPatchNotice(
              "Re-open this build to edit slides (its workspace is suspended).",
            );
          } else if (e instanceof ApiError && e.status === 422) {
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
    [cid, base],
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
    <div className="flex h-full min-h-0 flex-col">
      {patchNotice && (
        <p
          role="alert"
          className="shrink-0 border-b border-hairline bg-warn/10 px-body py-hair font-ui text-[0.78rem] text-warn"
        >
          {patchNotice}
        </p>
      )}
      <div className="relative min-h-0 flex-1">
        <DeckEditor deck={deck} disableDrag onPatch={handlePatch} />
        {saving && (
          // Covers the editor (pointer-events block) so a second edit can't be made
          // until the in-flight save persists — serializes writes, prevents lost updates.
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

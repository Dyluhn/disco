/**
 * R7: map the backend slide-deck renderer provenance to an HONEST label so a real
 * structured deck and a degraded HTML fallback are visibly distinguishable.
 * Previously everything non-"fallback" was mislabeled "Marp-rendered", hiding the
 * real C3 pipeline — the user couldn't tell a real deck from "HTML fake slides".
 *
 *   c3-brand / pptx-native → real, structured, editable
 *   libreoffice            → real PDF
 *   marp                   → real (image-based PPTX)
 *   fallback               → DEGRADED HTML (Marp CLI unavailable) — warn
 *
 * A free string (not a closed union) so a new backend renderer never silently
 * mislabels — it falls through to a neutral "rendered" rather than asserting "real".
 */
export function rendererLabel(renderer?: string): { real: boolean; short: string; long: string } {
  switch (renderer) {
    case "fallback":
      return {
        real: false,
        short: "HTML fallback",
        long: "the HTML fallback renderer — Marp unavailable, so deck quality is limited",
      };
    case "c3-brand":
    case "pptx-native":
      return { real: true, short: "structured (editable)", long: "the Disco structured renderer" };
    case "libreoffice":
      return { real: true, short: "LibreOffice PDF", long: "LibreOffice" };
    case "marp":
      return { real: true, short: "Marp-rendered", long: "Marp" };
    default:
      return { real: true, short: renderer || "rendered", long: renderer || "the slide renderer" };
  }
}

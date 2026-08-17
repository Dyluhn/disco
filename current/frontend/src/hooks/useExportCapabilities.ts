import { useEffect, useState } from "react";
import { agentGet } from "@/api/client";

export interface ExportCapabilities {
  md: boolean;
  pdf: boolean;
}

// Default: markdown only. PDF runs in the agent-server (weasyprint); when the
// server can't produce it — or we're offline / in fixture mode — we must NOT
// offer a button that would 500. So the honest default is md-only, and PDF
// lights up only when /api/export/capabilities says the server has it.
const MD_ONLY: ExportCapabilities = { md: true, pdf: false };

/** What the agent-server can actually export, probed once. No false affordance:
 *  the PDF button is gated on the REAL server capability, not a hardcoded flag —
 *  so it enables automatically wherever weasyprint is present. */
export function useExportCapabilities(): ExportCapabilities {
  const [caps, setCaps] = useState<ExportCapabilities>(MD_ONLY);
  useEffect(() => {
    let alive = true;
    agentGet<ExportCapabilities>("/api/export/capabilities")
      .then((c) => {
        if (alive) setCaps({ md: c.md !== false, pdf: !!c.pdf });
      })
      .catch(() => {
        /* offline / fixture / no endpoint → keep md-only */
      });
    return () => {
      alive = false;
    };
  }, []);
  return caps;
}

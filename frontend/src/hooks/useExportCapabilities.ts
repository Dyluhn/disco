import { useEffect, useState } from "react";
import { agentGet } from "@/api/client";

export interface ExportCapabilities {
  md: boolean;
  pdf: boolean;
  docx: boolean;
}

// Default: markdown only. PDF/DOCX run in the agent-server (weasyprint / pandoc);
// when the server can't produce them — or we're offline / in fixture mode — we
// must NOT offer a button that would 500. So the honest default is md-only, and
// PDF/DOCX light up only when /api/export/capabilities says the server has them.
const MD_ONLY: ExportCapabilities = { md: true, pdf: false, docx: false };

/** What the agent-server can actually export, probed once. No false affordance:
 *  the PDF/DOCX buttons are gated on the REAL server capability, not a hardcoded
 *  flag — so they enable automatically wherever weasyprint/pandoc are present. */
export function useExportCapabilities(): ExportCapabilities {
  const [caps, setCaps] = useState<ExportCapabilities>(MD_ONLY);
  useEffect(() => {
    let alive = true;
    agentGet<ExportCapabilities>("/api/export/capabilities")
      .then((c) => {
        if (alive) setCaps({ md: c.md !== false, pdf: !!c.pdf, docx: !!c.docx });
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

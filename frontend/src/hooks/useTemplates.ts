import { useEffect, useState } from "react";
import { agentGet } from "@/api/client";

/** One export template (a brand theme spin), shared by the DR-report PDF export
 *  and the slide-deck export selectors. Mirrors core.brand.TemplateInfo. */
export interface Template {
  id: string; // "{name}-{mode}", the selector value
  name: string;
  mode: "light" | "dark";
  label: string;
  description: string;
  accent: string; // swatch chroma (#hex)
  bg: string; // swatch canvas (#hex)
  default: boolean;
}

// The default is always present so the selector is never empty (offline / fixture).
const FALLBACK: Template[] = [
  {
    id: "disco-light",
    name: "disco",
    mode: "light",
    label: "Disco",
    description: "Warm editorial — Fraunces serif on paper-white, blue accent.",
    accent: "#4077a3",
    bg: "#fcfcfa",
    default: true,
  },
];

/** The export-template gallery from /api/templates (one source of truth shared by
 *  the PDF + slide-deck export UIs). Falls back to the Disco default offline. */
export function useTemplates(): Template[] {
  const [templates, setTemplates] = useState<Template[]>(FALLBACK);
  useEffect(() => {
    let alive = true;
    agentGet<{ templates: Template[] }>("/api/templates")
      .then((r) => {
        if (alive && Array.isArray(r.templates) && r.templates.length > 0) {
          setTemplates(r.templates);
        }
      })
      .catch(() => {
        /* offline / fixture / no endpoint → keep the Disco default */
      });
    return () => {
      alive = false;
    };
  }, []);
  return templates;
}

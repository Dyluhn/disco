import { Beaker, Landmark, Scale, Stethoscope } from "lucide-react";
import type { ComponentType } from "react";

/**
 * Empty-state example queries (Prompt 3B). Quiet, OUTLINED pills (icon + label) —
 * restrained like Manus's category pills, never gradient marketing cards. They
 * give the empty state calm identity and a way in without shouting. Clicking one
 * submits it as the query.
 */
interface Example {
  icon: ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  label: string;
  query: string;
}

const EXAMPLES: Example[] = [
  {
    icon: Beaker,
    label: "How RRF works",
    query: "How does reciprocal rank fusion work, and when should I use it?",
  },
  {
    icon: Scale,
    label: "Compare two licenses",
    query: "What are the practical differences between the MIT and Apache 2.0 licenses?",
  },
  {
    icon: Stethoscope,
    label: "Explain a study",
    query: "Summarize the current evidence on intermittent fasting and metabolic health.",
  },
  {
    icon: Landmark,
    label: "Trace a policy",
    query: "How has the EU AI Act's risk classification changed since the first draft?",
  },
];

export function ExampleQueries({ onPick }: { onPick: (query: string) => void }) {
  return (
    <div className="flex w-full max-w-measure flex-wrap justify-center gap-inline">
      {EXAMPLES.map((ex) => {
        const Icon = ex.icon;
        return (
          <button
            key={ex.label}
            type="button"
            onClick={() => onPick(ex.query)}
            className="flex items-center gap-hair rounded-control border border-hairline px-body py-inline font-ui text-[0.82rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text"
          >
            <Icon className="size-3.5 text-text-faint" aria-hidden />
            {ex.label}
          </button>
        );
      })}
    </div>
  );
}

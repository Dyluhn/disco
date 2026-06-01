import { CAPABILITY_LABEL, type Capability } from "@/types/models";

/**
 * Quiet capability badges — advisory metadata that informs assignment (never a
 * gate). Neutral styling: these are informational, not actionable, so no chroma.
 */
export function CapabilityBadges({ capabilities }: { capabilities: Capability[] }) {
  if (capabilities.length === 0) {
    return <span className="font-ui text-[0.72rem] text-text-faint">—</span>;
  }
  return (
    <span className="flex flex-wrap gap-hair">
      {capabilities.map((c) => (
        <span
          key={c}
          className="rounded-[0.25rem] border border-hairline px-1 py-px font-ui text-[0.66rem] text-text-muted"
        >
          {CAPABILITY_LABEL[c]}
        </span>
      ))}
    </span>
  );
}

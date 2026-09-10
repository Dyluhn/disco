/**
 * The isolation → confirmation coupling note for SandboxSection, surfaced for
 * the selected backend. Pulled out of the component purely to shed
 * cyclomatic complexity (TS-0035).
 */

import { ShieldHalf } from "lucide-react";
import { cn } from "@/lib/cn";
import type { BackendMeta } from "@/types/sandbox";

export function SandboxConfirmNote({
  meta,
}: {
  meta: BackendMeta | undefined;
}) {
  if (!meta) return null;
  return (
    <p
      className={cn(
        "flex items-start gap-hair rounded-control border px-body py-inline font-ui text-[0.8rem] leading-snug",
        meta.adversarialSafe
          ? "border-hairline text-text-muted"
          : "border-weak/50 text-text-muted",
      )}
    >
      <ShieldHalf
        className="mt-px size-3.5 shrink-0 text-text-faint"
        aria-hidden
      />
      {meta.confirmNote}
    </p>
  );
}

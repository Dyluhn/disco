import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown, History } from "lucide-react";
import type { WorkspaceVersion } from "@/api/agent";
import { cn } from "@/lib/cn";
import { triggerBadgeClass, versionLabel } from "./helpers";

export function VersionPicker({
  versions,
  selectedSeq,
  onSelect,
}: {
  versions: WorkspaceVersion[];
  selectedSeq: number | null;
  onSelect: (seq: number | null) => void;
}) {
  if (versions.length === 0) return null;
  const selected = selectedSeq == null ? null : versions.find((version) => version.seq === selectedSeq);
  return (
    <Dropdown.Root>
      <Dropdown.Trigger
        aria-label="Version history"
        className="flex min-h-11 max-w-[18rem] items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text lg:min-h-0"
      >
        <History className="size-3.5 shrink-0" aria-hidden />
        <span className="truncate">{selected ? `v${selected.seq}` : "Current"}</span>
        <ChevronDown className="size-3 shrink-0 opacity-60" aria-hidden />
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content
          align="end"
          sideOffset={6}
          className="z-50 min-w-[18rem] max-w-[min(28rem,92vw)] rounded-card border border-hairline bg-bg p-px shadow-none"
        >
          <Dropdown.Item
            onSelect={() => onSelect(null)}
            className="flex min-h-11 cursor-pointer items-center gap-inline rounded-control px-inline py-hair font-ui text-[0.78rem] text-text outline-none data-[highlighted]:bg-surface-2 lg:min-h-0"
          >
            <Check className={cn("size-3.5 shrink-0", selectedSeq === null ? "text-accent" : "opacity-0")} aria-hidden />
            <span className="min-w-0 flex-1 truncate">Current</span>
          </Dropdown.Item>
          <Dropdown.Separator className="my-px h-px bg-hairline" />
          {versions.map((version) => (
            <Dropdown.Item
              key={version.seq}
              onSelect={() => onSelect(version.seq)}
              className="flex min-h-11 cursor-pointer items-center gap-inline rounded-control px-inline py-hair font-ui text-[0.78rem] text-text outline-none data-[highlighted]:bg-surface-2 lg:min-h-0"
            >
              <Check className={cn("size-3.5 shrink-0", selectedSeq === version.seq ? "text-accent" : "opacity-0")} aria-hidden />
              <span className="min-w-0 flex-1 truncate">{versionLabel(version)}</span>
              <span className={cn("shrink-0 rounded-full border px-hair py-px font-ui text-[0.62rem] uppercase tracking-wide", triggerBadgeClass(version.trigger))}>
                {version.trigger}
              </span>
            </Dropdown.Item>
          ))}
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}

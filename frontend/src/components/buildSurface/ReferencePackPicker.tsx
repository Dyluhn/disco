/**
 * Reference pack picker for the Build options row: a multi-select over the
 * user's library. Only packs chosen here are bound to the Build (before its
 * first model call); nothing is selected automatically.
 */
import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { BookMarked, Check } from "lucide-react";
import { cn } from "@/lib/cn";
import { useReferencePacks } from "@/hooks/useReferencePacks";

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function ReferencePackPicker({
  selected,
  onChange,
  disabled = false,
  defaultOpen = false,
}: {
  selected: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
  /** Start expanded (tests and screenshots). */
  defaultOpen?: boolean;
}) {
  const { data: packs = [], isLoading } = useReferencePacks();
  const toggle = (id: string) =>
    onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);
  const selectedNames = packs.filter((p) => selected.includes(p.pack_id)).map((p) => p.name);

  return (
    <Dropdown.Root defaultOpen={defaultOpen}>
      <Dropdown.Trigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-label="Reference packs"
          data-disco-control="build.reference-packs"
          title="Reference packs: your saved file collections. Selected packs are copied into this build under references/ and the agent reads them before planning."
          className={cn(
            "flex min-h-11 items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.76rem] transition-colors lg:min-h-0",
            selected.length > 0
              ? "border-accent/50 bg-surface-1 text-accent"
              : "border-hairline text-text-muted hover:text-text",
            disabled && "opacity-60",
          )}
        >
          <BookMarked className="size-3.5 shrink-0" aria-hidden />
          {selected.length > 0 ? `References (${selected.length})` : "References"}
        </button>
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content
          align="start"
          sideOffset={6}
          className="z-50 w-80 rounded-card border border-hairline bg-surface-1 p-hair shadow-lg"
        >
          <div className="px-inline py-hair font-ui text-[0.7rem] uppercase tracking-wide text-text-faint">
            Reference packs
          </div>
          {isLoading && (
            <div className="px-inline py-hair font-ui text-[0.8rem] text-text-muted">Loading…</div>
          )}
          {!isLoading && packs.length === 0 && (
            <div className="px-inline py-inline font-ui text-[0.8rem] text-text-muted">
              No reference packs yet. In an Agent task, attach files and ask it to make a
              reference pack; manage packs under Settings → Reference packs.
            </div>
          )}
          {packs.map((pack) => {
            const on = selected.includes(pack.pack_id);
            return (
              <Dropdown.CheckboxItem
                key={pack.pack_id}
                checked={on}
                onCheckedChange={() => toggle(pack.pack_id)}
                onSelect={(e) => e.preventDefault()}
                className="flex cursor-pointer items-start gap-inline rounded-control px-inline py-hair outline-none data-[highlighted]:bg-surface-2"
              >
                <span
                  className={cn(
                    "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded border",
                    on ? "border-accent bg-accent text-surface-1" : "border-hairline",
                  )}
                  aria-hidden
                >
                  {on && <Check className="size-3" />}
                </span>
                <span className="flex min-w-0 flex-col">
                  <span className="truncate font-ui text-[0.84rem] text-text">{pack.name}</span>
                  {pack.description && (
                    <span className="line-clamp-2 font-ui text-[0.74rem] text-text-muted">
                      {pack.description}
                    </span>
                  )}
                  <span className="font-ui text-[0.7rem] text-text-faint">
                    {pack.file_count} file{pack.file_count === 1 ? "" : "s"} ·{" "}
                    {formatBytes(pack.total_bytes)} · updated{" "}
                    {new Date(pack.updated_at).toLocaleDateString()}
                  </span>
                </span>
              </Dropdown.CheckboxItem>
            );
          })}
          {selectedNames.length > 0 && (
            <div className="border-t border-hairline px-inline py-hair font-ui text-[0.72rem] text-text-muted">
              Selected: {selectedNames.join(", ")}
            </div>
          )}
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}

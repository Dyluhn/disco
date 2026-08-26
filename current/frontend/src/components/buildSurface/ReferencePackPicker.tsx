import { Check, Library } from "lucide-react";
import { referencePacksAvailable, type ReferencePack } from "@/api/referencePacks";
import { useReferencePacks } from "@/hooks/useReferencePacks";
import { cn } from "@/lib/cn";

export function ReferencePackPicker({
  selected,
  onChange,
}: {
  selected: ReferencePack[];
  onChange: (packs: ReferencePack[]) => void;
}) {
  const { data: packs, isLoading } = useReferencePacks();
  const selectedIds = new Set(selected.map((pack) => pack.id));
  const toggle = (pack: ReferencePack) => {
    onChange(
      selectedIds.has(pack.id)
        ? selected.filter((item) => item.id !== pack.id)
        : [...selected, pack],
    );
  };

  if (!referencePacksAvailable()) {
    return <span className="font-ui text-[0.76rem] text-text-faint">Reference Packs require the agent server.</span>;
  }
  if (isLoading) return <span className="font-ui text-[0.76rem] text-text-muted">Loading Reference Packs…</span>;
  if (!packs?.length) return <span className="font-ui text-[0.76rem] text-text-faint">No Reference Packs available.</span>;

  return (
    <div className="flex min-w-[min(100%,22rem)] flex-col gap-hair" aria-label="Reference Packs">
      <div className="flex items-center gap-hair font-ui text-[0.76rem] font-medium text-text-muted">
        <Library className="size-3.5" aria-hidden /> Reference Packs
      </div>
      <div className="flex flex-wrap gap-hair">
        {packs.map((pack) => {
          const checked = selectedIds.has(pack.id);
          return (
            <button
              key={pack.id}
              type="button"
              role="checkbox"
              aria-checked={checked}
              aria-label={`Use ${pack.name}`}
              onClick={() => toggle(pack)}
              className={cn(
                "flex min-h-11 items-center gap-hair rounded-control border px-inline py-hair text-left font-ui text-[0.76rem] transition-colors lg:min-h-0",
                checked ? "border-accent/50 bg-accent/10 text-text" : "border-hairline text-text-muted hover:border-accent/50 hover:text-text",
              )}
            >
              {checked && <Check className="size-3.5 text-accent" aria-hidden />}
              <span>{pack.name}</span>
              <span className="text-text-faint">({pack.files.length})</span>
            </button>
          );
        })}
      </div>
      {selected.length > 0 && <p className="font-ui text-[0.72rem] text-text-faint">Selected packs are snapshotted when this Build starts.</p>}
    </div>
  );
}

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAppKitControls, saveAppKitControls } from "@/api/agentAppkitOwner";
import type { TweakValue } from "@/types/appkitOwner";
import { AppCard } from "./AppCard";

export function AppKitOwnerPanel({ cid, status }: { cid: string; status: string }) {
  const client = useQueryClient();
  const key = ["appkit-controls", cid, status];
  const query = useQuery({ queryKey: key, queryFn: () => getAppKitControls(cid), retry: false });
  const controls = query.data?.controls;
  const [draft, setDraft] = useState<Record<string, TweakValue>>({});
  useEffect(() => { setDraft(controls?.values ?? {}); }, [controls]);
  const save = useMutation({
    mutationFn: () => {
      if (!controls) throw new Error("Owner controls are unavailable.");
      const changed = Object.fromEntries(Object.entries(draft).filter(([name, value]) => value !== controls.values[name]));
      return saveAppKitControls(cid, controls.spec_digest, changed);
    },
    onSuccess: async (data) => {
      client.setQueryData(key, data);
      await Promise.all([
        client.invalidateQueries({ queryKey: ["build-preview", cid] }),
        client.invalidateQueries({ queryKey: ["project-manifest", cid] }),
        client.invalidateQueries({ queryKey: ["project-release", cid] }),
      ]);
    },
  });
  if (!controls && !query.error) return null;
  const changed = controls && Object.entries(draft).some(([name, value]) => value !== controls.values[name]);
  const busy = save.isPending || query.isFetching;
  return <section aria-label="AppKit owner controls" className="flex flex-col gap-inline">
    {controls && <>
      <fieldset disabled={!controls.editable || busy}>
        <AppCard title={`${controls.title} — owner controls`} fields={controls.fields} values={draft}
          onTweak={(name, value) => setDraft(current => ({ ...current, [name]: value }))} />
      </fieldset>
      {!controls.editable && <p className="font-ui text-sm text-text-muted">Controls become editable when the build finishes.</p>}
      <button type="button" disabled={!controls.editable || busy || !changed}
        data-disco-control="build.tweaks-save" onClick={() => save.mutate()}
        className="rounded-control border border-hairline px-inline py-hair font-ui text-sm">
        {save.isPending ? "Saving and rebuilding…" : "Save changes"}
      </button>
    </>}
    {(query.error || save.error) && <div role="alert">
      <p>{(save.error ?? query.error)?.message ?? "Could not update owner controls."}</p>
      <button type="button" disabled={busy} onClick={() => { save.reset(); void query.refetch(); }}>
        Reload controls
      </button>
    </div>}
  </section>;
}

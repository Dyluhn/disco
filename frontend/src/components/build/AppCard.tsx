/**
 * AppCard — P9D. A thin presentational card for a built AppKit app: its title + the owner
 * TweakPanel. It passes its props straight through (no hardcoded demo tweaks, no no-op
 * callback) — the parent supplies the real fields/values and wires onTweak to app_set_tweak.
 */

import { TweakPanel, type TweakFieldView, type TweakValue } from "@/components/build/TweakPanel";

interface Props {
  title: string;
  fields: TweakFieldView[];
  values: Record<string, TweakValue>;
  onTweak: (key: string, value: TweakValue) => void;
}

export function AppCard({ title, fields, values, onTweak }: Props) {
  return (
    <div
      data-disco-control="build.appcard"
      className="flex flex-col gap-inline rounded-control border border-hairline bg-surface-1 p-body"
    >
      <h3 className="font-ui text-[0.82rem] font-medium text-text">{title}</h3>
      {fields.length > 0 ? (
        <TweakPanel fields={fields} values={values} onTweak={onTweak} />
      ) : (
        <span className="font-ui text-[0.72rem] text-text-faint">No owner controls for this app.</span>
      )}
    </div>
  );
}

/**
 * TweakPanel — P9D (owner controls UI).
 *
 * Renders ONE semantic control per tweak editor (boolean→checkbox, enum→select, int/float→
 * range slider, palette→curated swatch buttons, color→native picker, text→one bounded
 * input) — never a generic freeform text-box soup. A known editor whose constraints are
 * malformed (enum without options, palette without colors, numeric without min/max/step) and
 * any unknown editor fail closed to a disabled "unsupported" note with NO input.
 *
 * Decoupled: changes flow up via `onTweak(key, value)`; the parent adapts to the real
 * app_set_tweak wire call (the live mount is P1B-LIVE). TweakFieldView is a props mirror of
 * the Python core TweakField (TS can't import core/tweaks.py).
 */

import { cn } from "@/lib/cn";

export type TweakEditor = "boolean" | "enum" | "int" | "float" | "color" | "palette" | "text";
export type TweakValue = string | number | boolean;

export interface TweakFieldView {
  key: string;
  label: string;
  /** A documented editor; an unrecognized value renders the unsupported note (API boundary). */
  editor: TweakEditor;
  options?: string[]; // enum
  colors?: string[]; // palette swatches
  min?: number; // int/float
  max?: number;
  step?: number;
  maxLength?: number; // text bound (default 120)
}

interface Props {
  fields: TweakFieldView[];
  values: Record<string, TweakValue>;
  onTweak: (key: string, value: TweakValue) => void;
}

const notFiniteNum = (x: unknown): boolean => typeof x !== "number" || !Number.isFinite(x);

/** A known editor whose required constraints are missing/ill-typed → render disabled, not a
 * fallback box. Numeric bounds must be FINITE numbers (runtime JSON can carry null). */
function isMalformed(f: TweakFieldView): boolean {
  if (f.editor === "enum") return !f.options || f.options.length === 0;
  if (f.editor === "palette") return !f.colors || f.colors.length === 0;
  if (f.editor === "int" || f.editor === "float")
    return notFiniteNum(f.min) || notFiniteNum(f.max) || notFiniteNum(f.step);
  return false;
}

const INPUT_CLS = "rounded border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.74rem] text-text";

function Unsupported() {
  return <span className="font-ui text-[0.72rem] text-text-faint">unsupported control</span>;
}

function Control({
  field,
  value,
  onTweak,
}: {
  field: TweakFieldView;
  value: TweakValue | undefined;
  onTweak: Props["onTweak"];
}) {
  if (isMalformed(field)) return <Unsupported />;
  const { key, label } = field;
  switch (field.editor) {
    case "boolean":
      return (
        <input
          type="checkbox"
          aria-label={label}
          checked={value === true}
          onChange={(e) => onTweak(key, e.target.checked)}
        />
      );
    case "enum":
      return (
        <select
          aria-label={label}
          className={INPUT_CLS}
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onTweak(key, e.target.value)}
        >
          {field.options!.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      );
    case "int":
    case "float":
      return (
        <input
          type="range"
          aria-label={label}
          min={field.min}
          max={field.max}
          step={field.step}
          value={typeof value === "number" ? value : field.min}
          onChange={(e) => onTweak(key, Number(e.target.value))}
        />
      );
    case "color":
      return (
        <input
          type="color"
          aria-label={label}
          value={typeof value === "string" ? value : "#000000"}
          onChange={(e) => onTweak(key, e.target.value)}
        />
      );
    case "palette":
      return (
        <div role="group" aria-label={label} className="flex gap-hair">
          {field.colors!.map((c) => (
            <button
              key={c}
              type="button"
              aria-label={c}
              aria-pressed={value === c}
              onClick={() => onTweak(key, c)}
              style={{ backgroundColor: c }}
              className={cn(
                "h-5 w-5 rounded-control border",
                value === c ? "ring-2 ring-accent" : "border-hairline",
              )}
            />
          ))}
        </div>
      );
    case "text":
      return (
        <input
          type="text"
          aria-label={label}
          maxLength={field.maxLength ?? 120}
          className={INPUT_CLS}
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onTweak(key, e.target.value)}
        />
      );
    default:
      return <Unsupported />;
  }
}

export function TweakPanel({ fields, values, onTweak }: Props) {
  return (
    <div className="flex flex-col gap-inline">
      {fields.map((field) => (
        <div
          key={field.key}
          data-disco-control={`build.tweak.${field.key}`}
          data-tweak-key={field.key}
          className="flex items-center justify-between gap-inline"
        >
          <span className="font-ui text-[0.74rem] text-text-muted">{field.label}</span>
          <Control field={field} value={values[field.key]} onTweak={onTweak} />
        </div>
      ))}
    </div>
  );
}

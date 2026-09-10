/**
 * DefinitionMark — the disco Latin definition mark in-app component.
 *
 * Ported from development/notes/evidence/_definition.html:53-60 (the signed-off mockup).
 * Three scales: "masthead" (largest), "colophon" (medium, default), "footer"
 * (smallest).  Neutral-branded contexts (theme.branded === false) suppress the
 * mark entirely — it IS the brand, so it must not appear when brand is off.
 *
 * CSS custom properties --accent, --display, --reading, --text, --text-muted,
 * --text-faint, --hairline are consumed from the existing theme.css vars
 * (no new tokens needed).
 *
 * Usage:
 *   <DefinitionMark scale="colophon" />
 *   <DefinitionMark scale="masthead" className="my-4" />
 */

import type { ComponentProps } from "react";
import { cn } from "@/lib/cn";

export type DefinitionMarkScale = "masthead" | "colophon" | "footer";

// Scale drives font-size on the root element — all child proportions are em-based
// so they cascade correctly, matching _definition.html:43-45 (.s-lg/.s-md/.s-sm).
const SCALE_CLASS: Record<DefinitionMarkScale, string> = {
  masthead: "text-[15pt]",
  colophon: "text-[10pt]",
  footer: "text-[7pt]",
};

interface DefinitionMarkProps extends Omit<ComponentProps<"div">, "children"> {
  scale?: DefinitionMarkScale;
  /** When false (neutral mode) the mark is suppressed and null is returned. */
  branded?: boolean;
}

export function DefinitionMark({
  scale = "colophon",
  branded = true,
  className,
  ...props
}: DefinitionMarkProps) {
  if (!branded) return null;

  return (
    <div
      className={cn("inline-block", SCALE_CLASS[scale], className)}
      {...props}
    >
      {/* .l1 — headword + POS + IPA */}
      <div className="flex items-baseline gap-[0.42em] flex-wrap">
        {/* .hw — headword in Fraunces display */}
        <span
          style={{
            fontFamily: "var(--font-display)",
            fontWeight: 600,
            fontVariationSettings: "'opsz' 40, 'SOFT' 0, 'WONK' 0",
            fontSize: "2em",
            letterSpacing: "-0.014em",
            color: "var(--color-text)",
            lineHeight: 0.95,
          }}
        >
          disco
        </span>
        {/* .pos — part-of-speech label */}
        <span
          style={{
            fontFamily: "var(--font-ui)",
            fontWeight: 600,
            fontSize: "0.62em",
            letterSpacing: "0.15em",
            textTransform: "uppercase",
            color: "var(--color-text-faint)",
          }}
        >
          Latin
          <span
            style={{
              color: "var(--color-accent)",
              fontWeight: 700,
              padding: "0 0.12em",
            }}
          >
            &middot;
          </span>
          verb
        </span>
        {/* .ipa — phonetic transcription */}
        <span
          style={{
            fontFamily: "var(--font-ui)",
            fontWeight: 400,
            fontSize: "0.66em",
            letterSpacing: "0.02em",
            color: "var(--color-text-faint)",
          }}
        >
          /ˈdɪs.koː/
        </span>
      </div>

      {/* .rule — hairline with accent tick */}
      <div
        className="relative"
        style={{
          height: 1,
          background: "var(--color-hairline)",
          margin: "0.5em 0 0.48em",
        }}
      >
        <i
          aria-hidden
          style={{
            position: "absolute",
            left: 0,
            top: -1,
            width: "1.5em",
            height: 2,
            background: "var(--color-accent)",
            borderRadius: 1,
            display: "block",
          }}
        />
      </div>

      {/* .gloss — italic definition */}
      <div
        style={{
          fontFamily: "var(--font-reading)",
          fontStyle: "italic",
          fontSize: "1.04em",
          lineHeight: 1.38,
          color: "var(--color-text-muted)",
        }}
      >
        &ldquo;I learn; I become acquainted with.&rdquo;
      </div>

      {/* .root — etymology */}
      <div
        style={{
          fontFamily: "var(--font-ui)",
          fontWeight: 400,
          fontSize: "0.7em",
          letterSpacing: "0.01em",
          color: "var(--color-text-faint)",
          marginTop: "0.5em",
        }}
      >
        from{" "}
        <em
          style={{
            fontFamily: "var(--font-reading)",
            fontStyle: "italic",
            fontSize: "1.1em",
            color: "var(--color-text-muted)",
          }}
        >
          discere
        </em>{" "}
        &mdash; to learn
      </div>
    </div>
  );
}

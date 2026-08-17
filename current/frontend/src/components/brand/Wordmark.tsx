/**
 * Wordmark — the "Disco." logo text component.
 *
 * Ported from current/docs/evidence/_definition.html:90-92.  The wordmark is
 * `Disco` in Fraunces display (font-weight 600, optical-size 60, no SOFT/WONK)
 * followed by an accent-colored `.` dot.
 *
 * When `branded` is false (neutral mode) the dot loses accent colour (no
 * chroma) and renders in text-muted, but the wordmark itself is never fully
 * suppressed — callers that want to hide it in neutral mode should do so
 * conditionally.
 *
 * Usage:
 *   <Wordmark />
 *   <Wordmark className="text-lg" branded={false} />
 */

import type { ComponentProps } from "react";
import { cn } from "@/lib/cn";

interface WordmarkProps extends Omit<ComponentProps<"span">, "children"> {
  /** When false (neutral mode), the dot renders in text-muted (no chroma). */
  branded?: boolean;
}

export function Wordmark({ branded = true, className, ...props }: WordmarkProps) {
  return (
    <span
      style={{
        fontFamily: "var(--font-display)",
        fontWeight: 600,
        fontVariationSettings: "'opsz' 60, 'SOFT' 0, 'WONK' 0",
        letterSpacing: "-0.01em",
      }}
      className={cn(className)}
      {...props}
    >
      Disco
      <span
        style={{
          color: branded ? "var(--color-accent)" : "var(--color-text-muted)",
        }}
      >
        .
      </span>
    </span>
  );
}

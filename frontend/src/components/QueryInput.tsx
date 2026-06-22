import { ArrowUp } from "lucide-react";
import { type FormEvent, useState } from "react";
import { cn } from "@/lib/cn";
import type { ScopeId } from "@/shell/mode";
import { ModelLeaderPill } from "./ModelLeaderPill";
import { ScopeControl } from "./ScopeControl";
import { ThinkToggle } from "./ThinkToggle";

interface Props {
  onSubmit: (query: string) => void;
  busy?: boolean;
  autoFocus?: boolean;
  placeholder?: string;
  /** The control cluster — rendered when handlers are supplied (Prompt 3C). The
   *  cluster is model + scope + think; the MODE lives in the top slider, not here. */
  leaderId?: string | null;
  onLeaderChange?: (id: string | null) => void;
  scope?: ScopeId;
  onScopeChange?: (next: ScopeId) => void;
  think?: boolean;
  onThinkChange?: (next: boolean) => void;
  showControls?: boolean;
  /** Optional content rendered inside the card, after the controls row (fix-c
   *  #3). Lets callers put secondary controls (e.g. the depth-tier selector +
   *  hint) INSIDE the card border with the submit button, instead of as a
   *  sibling <div> floating below it. */
  footer?: React.ReactNode;
  /** R10: secondary controls rendered INLINE in the same flex-wrap pill row as
   *  the model/scope/think cluster (same size + register). Used by the Deep
   *  Research surface for Depth/Recency/Iterative so selecting DR doesn't add a
   *  new full-width row that grows the card and reflows the centered layout. */
  extraControls?: React.ReactNode;
  /** Gap #18: the stable `data-disco-control` id for the submit button. The
   *  shared QueryInput is mounted at TWO distinct sites (first send + the build
   *  surface's replan box); both previously collapsed onto `send-message`, so a
   *  HitMap keyed by controlId marked both covered after a single click. Callers
   *  pass a distinct id (e.g. "replan-send") to make the replan submit
   *  independently driveable + coverable. Defaults to "send-message" so the
   *  primary first-send site is unchanged. */
  controlId?: string;
  /** Gap #30: a STABLE accessible name for the textarea, independent of the
   *  (dynamic, caller-supplied) placeholder. `uiInventory` names form-inputs by
   *  aria-label first, so without this the main query field was unaddressable by
   *  the inventory. Defaults to a stable label; callers may override per surface. */
  inputAriaLabel?: string;
}

/**
 * The query field — now a GENEROUS input CARD (Prompt 3, gaps 2/3): real height +
 * internal padding (a substantial surface, not a thin bar), with the control
 * cluster (leader pill · mode selector · Think) on a quiet row beneath the
 * textarea, and a SOLID primary submit. Accepts vague, messy, multi-part
 * questions; no syntax, no tips (the backend expands silently — BoD §13.3).
 */
export function QueryInput({
  onSubmit,
  busy,
  autoFocus,
  placeholder,
  leaderId = null,
  onLeaderChange,
  scope = "standard",
  onScopeChange,
  think = false,
  onThinkChange,
  showControls = true,
  footer,
  extraControls,
  controlId = "send-message",
  inputAriaLabel = "Ask Disco a question",
}: Props) {
  const [value, setValue] = useState("");
  const canSend = !!value.trim() && !busy;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const q = value.trim();
    if (q && !busy) {
      onSubmit(q);
      setValue("");
    }
  };

  const controls = showControls && onLeaderChange && onScopeChange && onThinkChange;

  return (
    <form
      onSubmit={submit}
      className={cn(
        "flex w-full flex-col gap-inline rounded-card border border-hairline bg-surface-1 px-body py-body",
        "transition-colors focus-within:border-hairline-strong",
      )}
    >
      <textarea
        autoFocus={autoFocus}
        rows={2}
        value={value}
        aria-label={inputAriaLabel}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) submit(e);
        }}
        placeholder={placeholder ?? "Ask anything — vague or precise."}
        className="max-h-52 min-h-[3rem] w-full resize-none bg-transparent font-ui text-[1.05rem] leading-snug text-text outline-none placeholder:text-text-faint"
      />

      <div className="flex items-end justify-between gap-inline">
        {/* the control cluster — quiet, hairline-bordered, chroma only when active */}
        <div className="flex min-w-0 flex-wrap items-center gap-inline">
          {controls && (
            <>
              <ModelLeaderPill value={leaderId} onChange={onLeaderChange} />
              <ScopeControl value={scope} onChange={onScopeChange} />
              <ThinkToggle value={think} onChange={onThinkChange} />
            </>
          )}
          {extraControls}
        </div>

        {/* the primary action (item B) — ALWAYS the solid primary-action color so
            it never recedes; when there's nothing to send it merely dims, it does
            not turn into a grey/disabled-looking control. */}
        <button
          type="submit"
          disabled={!canSend}
          aria-label="Ask"
          data-disco-control={controlId}
          className={cn(
            "grid size-9 shrink-0 place-items-center rounded-control bg-accent text-bg transition-opacity",
            canSend ? "hover:opacity-90" : "opacity-50",
          )}
        >
          <ArrowUp className="size-4" aria-hidden />
        </button>
      </div>

      {/* Caller-supplied footer slot (fix-c #3). Sits INSIDE the card, after
          the controls row, so secondary selectors (e.g. depth tier) belong to
          the same visual unit as the submit button — not a stray sibling
          <div> below the card border. */}
      {footer}
    </form>
  );
}

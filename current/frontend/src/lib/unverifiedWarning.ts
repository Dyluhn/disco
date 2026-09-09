/**
 * UI-12 — the "unverified release" warning, rewritten for the human.
 *
 * When a run hands over work that its automatic verifier did not pass, the loop
 * emits an ENVIRONMENT message that is written AS A PROMPT TO THE MODEL:
 *
 *   "⚠ Finished WITHOUT a passing verify_web_app verdict — the deliverable is
 *    UNVERIFIED and may be INCOMPLETE. trusted components: database-kit probe:
 *    3/4 checks passed. — health_migration_version: body.migration_version =
 *    None … Note this clearly in your summary."
 *
 * That sentence has to keep working as a prompt (the backend tests pin its
 * phrases), so the fix lives HERE, at the presentation layer: the activity feed
 * renders a plain sentence derived from the raw text — which check didn't pass,
 * and whether the thing is still usable — and keeps the raw text verbatim
 * behind a disclosure so nothing is lost.
 *
 * It also deliberately does NOT say "Finished": the same card can be on screen
 * while the status chip still reads "Working" (the loop carries on after the
 * warning), and a card contradicting the chip is its own bug.
 */

/** The shape the raw environment warning is rewritten into. */
export interface UnverifiedWarning {
  /** One short line, safe to render bold at the top of the card. */
  headline: string;
  /** Two plain sentences: what didn't pass, and whether it's still usable. */
  body: string;
  /** The original environment text, verbatim, for the disclosure. */
  raw: string;
}

// Both producers (browser_gate.py and host_disposition.py) use the same frame:
// "⚠ Finished WITHOUT a passing <check> verdict — …".
const UNVERIFIED_RE = /^⚠\s*Finished WITHOUT a passing\s+(.+?)\s+verdict\b/;

/** Plain name for the verifier that didn't pass. */
function checkLabel(tool: string): string {
  if (tool.includes("verify_web_app") || tool.includes("browser")) {
    return "browser check";
  }
  if (tool.includes("host")) return "host check";
  return `${tool} check`;
}

/**
 * Rewrite an unverified-release warning for the user, or return null when the
 * text is some other ⚠ environment message (those still render as-is).
 */
export function unverifiedWarning(raw: string): UnverifiedWarning | null {
  const m = UNVERIFIED_RE.exec(raw.trim());
  if (!m) return null;
  const label = checkLabel(m[1]);
  return {
    headline: "One automatic check didn't pass",
    body:
      `The agent handed over the work without a passing ${label}, so this ` +
      "deliverable has not been verified and may be incomplete. It may still " +
      "work — open it in Preview, or download the source, and try it yourself.",
    raw,
  };
}

/**
 * UI-10 — sandbox-local addresses must never be handed to the user as if they
 * were links.
 *
 * The agent works inside a sandbox and narrates from inside it: "the site is
 * served via preview at http://localhost:8000/". That address is the SANDBOX's
 * own loopback. In the user's browser it resolves to the user's own machine —
 * it opens nothing, or worse, opens something unrelated. The real way in is the
 * Preview tab / Open button, which mints an isolated preview URL.
 *
 * This is a PRESENTATION-layer rewrite: the model's transcript is stored and
 * replayed untouched: only the rendered completion summary is scrubbed, so
 * nothing about the run's record changes.
 *
 * The loopback host set mirrors the backend's own preview-URL knowledge
 * (`packages/core/src/disco/core/loop/finish/_common_parts/preview_urls.py`
 * :: `_preview_key`, which collapses localhost / 127.0.0.1 / 0.0.0.0 / ::1 /
 * empty-host to one key). Any port is treated as sandbox-local: the preview
 * platform assigns a RANDOM port, so there is no fixed :8000 to special-case.
 */

/** A loopback authority: localhost | 127.0.0.1 | 0.0.0.0 | [::1] | ::1, plus an
 *  optional :port. Not anchored so it can be embedded in the URL patterns. */
const LOOPBACK_HOST = String.raw`(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|::1)(?::\d{1,5})?`;

/** A whole sandbox-local URL: scheme + loopback authority + optional path/query. */
const SANDBOX_URL = String.raw`https?:\/\/${LOOPBACK_HOST}(?:\/[^\s<>()[\]"'\`]*)?`;

/** What the user should use instead. Phrased to read naturally in the places the
 *  agent puts a URL ("served at …", "open …", "visit …"). */
const PREVIEW_PHRASE = "the Preview tab";

/** Ordered rewrites: the enclosing markdown syntax is consumed FIRST so a link,
 *  autolink or code span collapses to plain words rather than leaving a
 *  `[the Preview tab](the Preview tab)` husk behind. */
const REWRITES: readonly RegExp[] = [
  // [label](http://localhost:8000/) — markdown link
  new RegExp(String.raw`\[[^\]]*\]\(\s*${SANDBOX_URL}\s*\)`, "gi"),
  // <http://localhost:8000/> — markdown autolink
  new RegExp(String.raw`<\s*${SANDBOX_URL}\s*>`, "gi"),
  // `http://localhost:8000/` — code span
  new RegExp(String.raw`\`\s*${SANDBOX_URL}\s*\``, "gi"),
  // bare URL
  new RegExp(SANDBOX_URL, "gi"),
];

export interface ScrubbedSummary {
  /** The summary text with every sandbox-local address replaced. */
  text: string;
  /** True when at least one address was replaced — the caller shows the note. */
  replaced: boolean;
}

/**
 * Replace every sandbox-local URL in a user-facing summary with a pointer at the
 * Preview tab. Pure; returns the input unchanged (and `replaced: false`) when
 * there is nothing sandbox-local in it.
 */
export function scrubSandboxUrls(text: string): ScrubbedSummary {
  let out = text;
  let replaced = false;
  for (const re of REWRITES) {
    out = out.replace(re, (match) => {
      replaced = true;
      // A URL at the end of a sentence swallows the punctuation into its path
      // ("…at http://localhost:8000/."). Hand it back so the prose still reads.
      const tail = /[.,;:!?]+$/.exec(match);
      return tail ? PREVIEW_PHRASE + tail[0] : PREVIEW_PHRASE;
    });
  }
  return { text: out, replaced };
}

/** The one-line explanation shown under a summary that was rewritten. Exported so
 *  the two render sites (live Build surface + the read-only run view) and their
 *  tests share ONE string. */
export const SANDBOX_URL_NOTE =
  "Disco replaced a sandbox-only address in this summary — open the site with the Preview tab or the Open button above.";

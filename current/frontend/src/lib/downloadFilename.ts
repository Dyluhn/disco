/**
 * Download filenames a person can recognise.
 *
 * A saved zip used to be named by the conversation id alone (`conv_8b9b….zip`),
 * so a downloads folder full of builds was unreadable. The name now leads with
 * the project's own title and keeps a short id suffix so two builds of the same
 * brief never collide:
 *
 *     coffee-shop-subscription-website-8b9b1c2d.zip
 *
 * The slug is deliberately conservative — lowercase ASCII letters, digits and
 * hyphens only — so the name is a legal filename on Linux, macOS and Windows
 * alike (no spaces, no `:`/`/`/`\`/`?`/`*`/`"`/`<`/`>`/`|`, no trailing dot).
 * A title that slugifies to nothing (emoji-only, CJK-only, punctuation) falls
 * back to the conversation id, which is always safe.
 */

/** Longest slug we keep. Leaves room for the `-<8 hex>` suffix and the
 *  extension well inside every filesystem's 255-byte component limit. */
const MAX_SLUG_CHARS = 60;

/** Lowercase ASCII-and-hyphen slug of a human title, or "" when nothing usable
 *  survives (so callers can fall back rather than emit a bare separator). */
export function slugifyTitle(title: string): string {
  return title
    .normalize("NFKD")
    // Drop combining marks left by the decomposition ("é" → "e").
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, MAX_SLUG_CHARS)
    .replace(/-+$/g, "");
}

/** The short, human-quotable form of a conversation id: `conv_8b9b1c2d…` → `8b9b1c2d`. */
export function shortConversationId(cid: string): string {
  return cid.replace(/^conv_/, "").slice(0, 8) || cid;
}

/** The base filename (no extension) for a project download. Title-led when the
 *  project has a usable title, the raw conversation id otherwise. */
export function projectDownloadBasename(cid: string, title?: string | null): string {
  const slug = title ? slugifyTitle(title) : "";
  return slug ? `${slug}-${shortConversationId(cid)}` : cid;
}

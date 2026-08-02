/**
 * NeedMoreCard — filename sanitization (extracted verbatim from
 * NeedMoreCard.tsx).
 */

export function sanitizeFilename(name: string): string {
  return (
    (name || "research-report")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60) || "research-report"
  );
}

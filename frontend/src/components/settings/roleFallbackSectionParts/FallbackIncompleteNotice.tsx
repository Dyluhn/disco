/**
 * The "set both a base URL and model" warning for RoleFallbackSection.
 * Pulled out of the component purely to shed cyclomatic complexity (TS-0034).
 */

export function FallbackIncompleteNotice({
  incomplete,
}: {
  incomplete: boolean;
}) {
  if (!incomplete) return null;
  return (
    <p className="font-ui text-[0.8rem] text-warn" role="status">
      Set both a base URL and model before saving an enabled fallback.
    </p>
  );
}

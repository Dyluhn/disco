/**
 * The signature time-to-first-token micro-interaction (BoD §13.7) — three
 * hairline bars settling into place like a subway map drawing itself. NOT a
 * spinner: restrained, on-brand, memorable. Honors prefers-reduced-motion (the
 * keyframe is disabled via the [data-ttft] rule in theme.css).
 */
export function TtftIndicator({ label = "Searching sources" }: { label?: string }) {
  return (
    <div data-ttft className="flex items-center gap-inline" role="status" aria-live="polite">
      <div className="flex h-4 w-7 flex-col justify-between">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="h-[2px] origin-left rounded-full bg-accent"
            style={{
              animation: "pmx-settle 1.25s ease-in-out infinite",
              animationDelay: `${i * 0.18}s`,
              opacity: 0.85 - i * 0.18,
            }}
          />
        ))}
      </div>
      <span className="font-ui text-[0.82rem] text-text-muted">{label}…</span>
    </div>
  );
}

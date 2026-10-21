import { useCallback, useEffect, useRef, useState } from "react";

/** A UI deadline, never authorization. Wall time catches sleep; monotonic time
 * prevents a backwards clock adjustment extending an active launch. */
export function usePreviewAccess(leaseDeadline: number | undefined) {
  const clock = useRef({ wall: Date.now(), monotonic: performance.now() });
  const [deadline, setDeadline] = useState<number | null>(leaseDeadline ?? null);
  const [now, setNow] = useState(Date.now);
  const accepted = useRef(false);
  const acceptDeadline = useCallback((expires: unknown, serverTime: unknown) => {
    if (accepted.current || typeof expires !== "number" || !Number.isFinite(expires) ||
      typeof serverTime !== "number" || !Number.isFinite(serverTime) ||
      !Number.isInteger(expires) || expires < 0 || serverTime < 0) return;
    accepted.current = true;
    // Anchor at launch start, not message receipt: delivery latency must never
    // move the UI deadline later than the cookie's server-issued expiry.
    const cookieDeadline = clock.current.wall + Math.max(0, expires - serverTime) * 1000;
    setDeadline((current) => current === null ? cookieDeadline : Math.min(current, cookieDeadline));
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const update = () => {
      clearTimeout(timer);
      const current = Math.max(Date.now(), clock.current.wall + performance.now() - clock.current.monotonic);
      setNow((previous) => Math.max(previous, current));
      if (deadline !== null && current < deadline) {
        const next = current < deadline - 60_000 ? deadline - 60_000 : deadline;
        timer = setTimeout(update, Math.min(2_147_483_647, Math.max(1, next - current)));
      }
    };
    update();
    window.addEventListener("focus", update);
    document.addEventListener("visibilitychange", update);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("focus", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, [deadline]);

  const phase = deadline === null ? "unknown" : now >= deadline ? "expired"
    : now >= deadline - 60_000 ? "warning" : "active";
  return { phase, acceptDeadline };
}

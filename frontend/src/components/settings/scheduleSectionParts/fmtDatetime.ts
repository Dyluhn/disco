/**
 * Locale-formats an ISO run time in a given IANA timezone — relocated out of
 * ScheduleSection.tsx (still a private, non-exported implementation detail)
 * so ConfirmCard and ScheduleList can share it.
 */

export function fmtDatetime(iso: string, timezone: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "short",
      timeZone: timezone,
    });
  } catch {
    return iso;
  }
}

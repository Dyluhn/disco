/** Browser IANA zone used to give cron fields explicit local wall-clock semantics. */
export function browserScheduleTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

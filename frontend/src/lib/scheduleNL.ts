/**
 * RP-08 client-side NL → cron helper.
 *
 * Converts natural-language schedule expressions to 5-field cron strings.
 * Falls back to literal pass-through for expressions that already look like
 * cron. Returns a NLParseResult — never throws, never guesses silently.
 *
 * Parse order:
 *   1. Detect existing cron expression (already formatted) → pass through.
 *   2. RFC-2445 RRULE-ish ("FREQ=DAILY;BYHOUR=9") → convert to cron.
 *   3. Natural-language patterns → map to cron.
 *   4. Nothing matched → error (never silent).
 */

export interface NLParseResult {
  success: boolean;
  rrule: string | null;      // validated cron expression when success=true
  description: string;       // human-readable label to show the user
  error: string | null;      // present when success=false
}

// Matches a 5-field cron expression like "*/5 * * * *" or "0 9 * * 1-5"
const CRON_RE = /^[\d/*,\-? ]+$/;
function looksLikeCron(s: string): boolean {
  const parts = s.trim().split(/\s+/);
  return parts.length === 5 && CRON_RE.test(s);
}

// ---- RFC-2445 RRULE-ish parsing ------------------------------------------

interface RruleParts {
  freq?: string;
  interval?: number;
  byhour?: number;
  byminute?: number;
  byday?: string;
}

function parseRruleParts(s: string): RruleParts | null {
  if (!s.toUpperCase().startsWith("FREQ=")) return null;
  const parts: RruleParts = {};
  for (const seg of s.split(";")) {
    const [key, val] = seg.split("=");
    if (!key || val === undefined) continue;
    switch (key.toUpperCase()) {
      case "FREQ":     parts.freq = val.toUpperCase(); break;
      case "INTERVAL": parts.interval = parseInt(val, 10); break;
      case "BYHOUR":   parts.byhour = parseInt(val, 10); break;
      case "BYMINUTE": parts.byminute = parseInt(val, 10); break;
      case "BYDAY":    parts.byday = val.toUpperCase(); break;
    }
  }
  return parts.freq ? parts : null;
}

const BYDAY_TO_DOW: Record<string, number> = {
  SU: 0, MO: 1, TU: 2, WE: 3, TH: 4, FR: 5, SA: 6,
};

function rrulePartsToCron(r: RruleParts): { cron: string; desc: string } | null {
  const min = r.byminute ?? 0;
  const hour = r.byhour ?? 9;
  const interval = r.interval ?? 1;

  switch (r.freq) {
    case "MINUTELY": {
      const step = interval === 1 ? "*" : `*/${interval}`;
      return { cron: `${step} * * * *`, desc: interval === 1 ? "every minute" : `every ${interval} minutes` };
    }
    case "HOURLY": {
      const step = interval === 1 ? "*" : `*/${interval}`;
      return { cron: `${min} ${step} * * *`, desc: interval === 1 ? "every hour" : `every ${interval} hours` };
    }
    case "DAILY": {
      const step = interval === 1 ? "*" : `*/${interval}`;
      return { cron: `${min} ${hour} ${step === "*" ? "*" : `1/${interval}`} * *`, desc: interval === 1 ? `daily at ${fmtHour(hour)}` : `every ${interval} days at ${fmtHour(hour)}` };
    }
    case "WEEKLY": {
      if (r.byday) {
        const dow = BYDAY_TO_DOW[r.byday];
        if (dow === undefined) return null;
        return { cron: `${min} ${hour} * * ${dow}`, desc: `every ${r.byday} at ${fmtHour(hour)}` };
      }
      return { cron: `${min} ${hour} * * 1`, desc: `weekly on Monday at ${fmtHour(hour)}` };
    }
    case "MONTHLY": {
      return { cron: `${min} ${hour} 1 * *`, desc: `monthly on the 1st at ${fmtHour(hour)}` };
    }
    default:
      return null;
  }
}

// ---- NL pattern table -------------------------------------------------------

function fmtHour(h: number): string {
  if (h === 0) return "12:00 AM";
  if (h === 12) return "12:00 PM";
  return h < 12 ? `${h}:00 AM` : `${h - 12}:00 PM`;
}

interface NLPattern {
  pattern: RegExp;
  toCron: (m: RegExpMatchArray) => { cron: string; desc: string } | null;
}

const NL_PATTERNS: NLPattern[] = [
  // "every N minutes"
  {
    pattern: /^every\s+(\d+)\s+min(?:ute)?s?$/i,
    toCron: (m) => ({ cron: `*/${m[1]} * * * *`, desc: `every ${m[1]} minutes` }),
  },
  // "every minute"
  {
    pattern: /^every\s+minute$/i,
    toCron: () => ({ cron: "* * * * *", desc: "every minute" }),
  },
  // "every N hours"
  {
    pattern: /^every\s+(\d+)\s+hours?$/i,
    toCron: (m) => ({ cron: `0 */${m[1]} * * *`, desc: `every ${m[1]} hours` }),
  },
  // "every hour"
  {
    pattern: /^every\s+hour$/i,
    toCron: () => ({ cron: "0 * * * *", desc: "every hour" }),
  },
  // "every day at HH(:MM)?"
  {
    pattern: /^every\s+day\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/i,
    toCron: (m) => {
      let h = parseInt(m[1], 10);
      const min = m[2] ? parseInt(m[2], 10) : 0;
      const ampm = (m[3] || "").toLowerCase();
      if (ampm === "pm" && h < 12) h += 12;
      if (ampm === "am" && h === 12) h = 0;
      return { cron: `${min} ${h} * * *`, desc: `daily at ${fmtHour(h)}` };
    },
  },
  // "daily at HH"
  {
    pattern: /^daily\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/i,
    toCron: (m) => {
      let h = parseInt(m[1], 10);
      const min = m[2] ? parseInt(m[2], 10) : 0;
      const ampm = (m[3] || "").toLowerCase();
      if (ampm === "pm" && h < 12) h += 12;
      if (ampm === "am" && h === 12) h = 0;
      return { cron: `${min} ${h} * * *`, desc: `daily at ${fmtHour(h)}` };
    },
  },
  // "every <weekday>"
  {
    pattern: /^every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$/i,
    toCron: (m) => {
      const dayMap: Record<string, number> = {
        sunday: 0, monday: 1, tuesday: 2, wednesday: 3,
        thursday: 4, friday: 5, saturday: 6,
      };
      const dow = dayMap[m[1].toLowerCase()];
      const day = m[1].charAt(0).toUpperCase() + m[1].slice(1).toLowerCase();
      return { cron: `0 9 * * ${dow}`, desc: `every ${day} at 9:00 AM` };
    },
  },
  // "every weekday"
  {
    pattern: /^every\s+weekday$/i,
    toCron: () => ({ cron: "0 9 * * 1-5", desc: "every weekday at 9:00 AM" }),
  },
  // "every weekend"
  {
    pattern: /^every\s+weekend$/i,
    toCron: () => ({ cron: "0 9 * * 0,6", desc: "every weekend at 9:00 AM" }),
  },
  // "weekly on <weekday> at HH"
  {
    pattern: /^weekly\s+on\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/i,
    toCron: (m) => {
      const dayMap: Record<string, number> = {
        sunday: 0, monday: 1, tuesday: 2, wednesday: 3,
        thursday: 4, friday: 5, saturday: 6,
      };
      const dow = dayMap[m[1].toLowerCase()];
      let h = parseInt(m[2], 10);
      const min = m[3] ? parseInt(m[3], 10) : 0;
      const ampm = (m[4] || "").toLowerCase();
      if (ampm === "pm" && h < 12) h += 12;
      if (ampm === "am" && h === 12) h = 0;
      const day = m[1].charAt(0).toUpperCase() + m[1].slice(1).toLowerCase();
      return { cron: `${min} ${h} * * ${dow}`, desc: `every ${day} at ${fmtHour(h)}` };
    },
  },
  // "monthly on the Nth"
  {
    pattern: /^monthly\s+on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?$/i,
    toCron: (m) => ({ cron: `0 9 ${m[1]} * *`, desc: `monthly on the ${m[1]} at 9:00 AM` }),
  },
  // "every N days"
  {
    pattern: /^every\s+(\d+)\s+days?$/i,
    toCron: (m) => {
      const n = parseInt(m[1], 10);
      return { cron: `0 9 */${n} * *`, desc: `every ${n} days at 9:00 AM` };
    },
  },
];

// ---- main entry point -------------------------------------------------------

/**
 * Parse a natural-language or cron-style schedule expression.
 *
 * Never throws. Returns `success: false` with an `error` message when the
 * expression cannot be understood — do NOT guess a schedule.
 */
export function parseScheduleNL(input: string): NLParseResult {
  const trimmed = input.trim();
  if (!trimmed) {
    return { success: false, rrule: null, description: "", error: "Schedule expression is empty." };
  }

  // 1. Already a cron expression?
  if (looksLikeCron(trimmed)) {
    return { success: true, rrule: trimmed, description: trimmed, error: null };
  }

  // 2. RFC-2445 RRULE-ish?
  const rruleParts = parseRruleParts(trimmed);
  if (rruleParts) {
    const result = rrulePartsToCron(rruleParts);
    if (result) {
      return { success: true, rrule: result.cron, description: result.desc, error: null };
    }
    return {
      success: false,
      rrule: null,
      description: "",
      error: `Could not convert RRULE "${trimmed}" to a schedule. Supported: FREQ=MINUTELY/HOURLY/DAILY/WEEKLY/MONTHLY with optional INTERVAL, BYHOUR, BYMINUTE, BYDAY.`,
    };
  }

  // 3. Natural-language patterns
  for (const { pattern, toCron } of NL_PATTERNS) {
    const m = trimmed.match(pattern);
    if (m) {
      const result = toCron(m);
      if (result) {
        return { success: true, rrule: result.cron, description: result.desc, error: null };
      }
    }
  }

  // 4. Nothing matched — never silent
  return {
    success: false,
    rrule: null,
    description: "",
    error: `Could not parse "${trimmed}" as a schedule. Try: "every day at 9am", "every Monday", "every 2 hours", "0 9 * * 1" (cron), or "FREQ=DAILY;BYHOUR=9" (RRULE).`,
  };
}

/**
 * Tests for scheduleNL.ts — WALK-17 (G3).
 *
 * Covers:
 *   1. Each ScheduleSection preset emits a cron that parseScheduleNL accepts.
 *   2. looksLikeCron range validation: rejects out-of-range fields before the
 *      network round-trip (was charset+count only; now validates each field).
 *   3. Valid cron expressions are still accepted.
 */

import { describe, it, expect } from "vitest";
import { parseScheduleNL } from "./scheduleNL";
import { SCHEDULE_PRESETS } from "@/components/settings/ScheduleSection";

// ---------------------------------------------------------------------------
// 1. Preset cron strings — every preset must parse as valid cron
// ---------------------------------------------------------------------------

describe("SCHEDULE_PRESETS — each preset emits valid cron", () => {
  for (const preset of SCHEDULE_PRESETS) {
    it(`accepts preset "${preset.label}" → "${preset.cron}"`, () => {
      const result = parseScheduleNL(preset.cron);
      expect(result.success).toBe(true);
      expect(result.error).toBeNull();
      // The cron expression is passed through verbatim
      expect(result.rrule).toBe(preset.cron);
    });
  }
});

// ---------------------------------------------------------------------------
// 2. Range validation — out-of-range values must be rejected (fails-before)
// ---------------------------------------------------------------------------

describe("looksLikeCron — rejects out-of-range field values", () => {
  it("rejects minute > 59 (99 9 * * *)", () => {
    const result = parseScheduleNL("99 9 * * *");
    expect(result.success).toBe(false);
    expect(result.error).not.toBeNull();
  });

  it("rejects hour > 23 (0 25 * * *)", () => {
    const result = parseScheduleNL("0 25 * * *");
    expect(result.success).toBe(false);
    expect(result.error).not.toBeNull();
  });

  it("rejects dom > 31 (0 9 32 * *)", () => {
    const result = parseScheduleNL("0 9 32 * *");
    expect(result.success).toBe(false);
    expect(result.error).not.toBeNull();
  });

  it("rejects dom < 1 (0 9 0 * *)", () => {
    const result = parseScheduleNL("0 9 0 * *");
    expect(result.success).toBe(false);
    expect(result.error).not.toBeNull();
  });

  it("rejects month > 12 (0 9 * 13 *)", () => {
    const result = parseScheduleNL("0 9 * 13 *");
    expect(result.success).toBe(false);
    expect(result.error).not.toBeNull();
  });

  it("rejects month < 1 (0 9 * 0 *)", () => {
    const result = parseScheduleNL("0 9 * 0 *");
    expect(result.success).toBe(false);
    expect(result.error).not.toBeNull();
  });

  it("rejects dow > 7 (0 9 * * 8)", () => {
    const result = parseScheduleNL("0 9 * * 8");
    expect(result.success).toBe(false);
    expect(result.error).not.toBeNull();
  });

  it("rejects both minute and hour out-of-range (99 99 * * *)", () => {
    const result = parseScheduleNL("99 99 * * *");
    expect(result.success).toBe(false);
    expect(result.error).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 3. Valid cron expressions — must still be accepted (passes-after)
// ---------------------------------------------------------------------------

describe("looksLikeCron — accepts valid cron expressions", () => {
  it("accepts basic cron (0 9 * * 1)", () => {
    const result = parseScheduleNL("0 9 * * 1");
    expect(result.success).toBe(true);
    expect(result.rrule).toBe("0 9 * * 1");
  });

  it("accepts wildcard in all fields (* * * * *)", () => {
    const result = parseScheduleNL("* * * * *");
    expect(result.success).toBe(true);
  });

  it("accepts */N step in hour (0 */6 * * *)", () => {
    const result = parseScheduleNL("0 */6 * * *");
    expect(result.success).toBe(true);
    expect(result.rrule).toBe("0 */6 * * *");
  });

  it("accepts weekday range in dow (0 9 * * 1-5)", () => {
    const result = parseScheduleNL("0 9 * * 1-5");
    expect(result.success).toBe(true);
    expect(result.rrule).toBe("0 9 * * 1-5");
  });

  it("accepts comma list in dow (0 9 * * 1,3,5)", () => {
    const result = parseScheduleNL("0 9 * * 1,3,5");
    expect(result.success).toBe(true);
    expect(result.rrule).toBe("0 9 * * 1,3,5");
  });

  it("accepts minute step (*/5 * * * *)", () => {
    const result = parseScheduleNL("*/5 * * * *");
    expect(result.success).toBe(true);
    expect(result.rrule).toBe("*/5 * * * *");
  });

  it("accepts max valid values (59 23 31 12 7)", () => {
    const result = parseScheduleNL("59 23 31 12 7");
    expect(result.success).toBe(true);
  });

  it("accepts min valid values for dom/month (0 0 1 1 0)", () => {
    const result = parseScheduleNL("0 0 1 1 0");
    expect(result.success).toBe(true);
  });
});

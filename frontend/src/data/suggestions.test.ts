import { describe, expect, it } from "vitest";
import {
  getSuggestionPool,
  getSuggestions,
  sampleSuggestions,
  SUGGESTION_POOLS,
  SUGGESTION_SAMPLE_MAX,
  SUGGESTION_SAMPLE_MIN,
  type SuggestionSurface,
} from "./suggestions";

function seededRandom(seed = 1) {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 0x100000000;
  };
}

function texts(surface: SuggestionSurface) {
  return new Set(getSuggestionPool(surface).map((suggestion) => suggestion.text));
}

describe("suggestion pools", () => {
  it("keeps curated pool sizes in the requested ranges", () => {
    expect(SUGGESTION_POOLS.search.length).toBeGreaterThanOrEqual(40);
    expect(SUGGESTION_POOLS.search.length).toBeLessThanOrEqual(60);
    expect(SUGGESTION_POOLS.build.length).toBeGreaterThanOrEqual(25);
    expect(SUGGESTION_POOLS.build.length).toBeLessThanOrEqual(40);
    expect(SUGGESTION_POOLS.agent.length).toBeGreaterThanOrEqual(25);
    expect(SUGGESTION_POOLS.agent.length).toBeLessThanOrEqual(40);
  });

  it("does not duplicate suggestions inside a sampled row", () => {
    const sample = sampleSuggestions(
      getSuggestionPool("search"),
      SUGGESTION_SAMPLE_MAX,
      seededRandom(42),
    );
    const ids = new Set(sample.map((suggestion) => suggestion.id));
    expect(sample).toHaveLength(SUGGESTION_SAMPLE_MAX);
    expect(ids.size).toBe(sample.length);
  });

  it("draws a 4-6 item row for component callers", () => {
    const sample = getSuggestions("agent");
    expect(sample.length).toBeGreaterThanOrEqual(SUGGESTION_SAMPLE_MIN);
    expect(sample.length).toBeLessThanOrEqual(SUGGESTION_SAMPLE_MAX);
  });

  it("keeps surface-specific pools", () => {
    const search = texts("search");
    const deep = texts("deep_research");
    const build = texts("build");
    const agent = texts("agent");

    expect(search.has("a 2048 clone")).toBe(false);
    expect(build.has("a 2048 clone")).toBe(true);
    expect(agent.has("browse example.com and extract pricing")).toBe(true);
    expect(build.has("browse example.com and extract pricing")).toBe(false);
    expect(deep.has("Produce a market map of solid-state battery commercialization with source tiers")).toBe(
      true,
    );
    expect(search.has("Produce a market map of solid-state battery commercialization with source tiers")).toBe(
      false,
    );
  });
});

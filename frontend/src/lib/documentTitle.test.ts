/**
 * UI-28: every route reported the same static "Disco — research", so tabs,
 * bookmarks and history entries were indistinguishable.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  documentTitle,
  getConversationTitle,
  publishConversationTitle,
  subscribeConversationTitle,
} from "./documentTitle";

describe("documentTitle", () => {
  beforeEach(() => publishConversationTitle(null));

  it("names the route, not the app", () => {
    expect(documentTitle("/settings", "search", null)).toBe("Settings — Disco");
    expect(documentTitle("/history", "search", null)).toBe("History — Disco");
    expect(documentTitle("/projects", "build", null)).toBe("Projects — Disco");
    expect(documentTitle("/deep/abc123", "search", null)).toBe(
      "Deep Research — Disco",
    );
    expect(documentTitle("/build/abc123", "build", null)).toBe("Build — Disco");
  });

  it("follows the mode slider on the main surface", () => {
    expect(documentTitle("/", "search", null)).toBe("Search — Disco");
    expect(documentTitle("/", "build", null)).toBe("Build — Disco");
    expect(documentTitle("/", "agent", null)).toBe("Agent — Disco");
  });

  it("prefers the open conversation's own title", () => {
    expect(documentTitle("/build/abc123", "build", "Recipe site")).toBe(
      "Recipe site — Disco",
    );
  });

  it("shortens a title too long to read in a tab", () => {
    const question = "How did ".repeat(20);

    const title = documentTitle("/deep/abc", "search", question);

    expect(title.endsWith(" — Disco")).toBe(true);
    expect(title.length).toBeLessThanOrEqual(60 + " — Disco".length);
    expect(title).toContain("…");
  });

  it("falls back to the route when a surface publishes a blank title", () => {
    expect(documentTitle("/build/abc123", "build", "   ")).toBe("Build — Disco");
  });
});

describe("conversation title seam", () => {
  beforeEach(() => publishConversationTitle(null));

  it("notifies subscribers only when the title actually changes", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeConversationTitle(listener);

    publishConversationTitle("Recipe site");
    publishConversationTitle("Recipe site");

    expect(getConversationTitle()).toBe("Recipe site");
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it("clears back to null so the next route names itself", () => {
    publishConversationTitle("Recipe site");

    publishConversationTitle(null);

    expect(getConversationTitle()).toBeNull();
  });
});

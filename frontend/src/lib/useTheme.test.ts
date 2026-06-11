import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useTheme } from "./useTheme";

describe("useTheme", () => {
  beforeEach(() => {
    document.documentElement.classList.remove("light", "dark");
    localStorage.clear();
  });

  it("should initialize with dark if no preference or storage", () => {
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");
  });

  it("should toggle theme and persist to localStorage", () => {
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");

    act(() => {
      result.current.toggle();
    });

    expect(result.current.theme).toBe("light");
    expect(localStorage.getItem("pmx-theme")).toBe("light");
    expect(document.documentElement.classList.contains("light")).toBe(true);

    act(() => {
      result.current.toggle();
    });

    expect(result.current.theme).toBe("dark");
    expect(localStorage.getItem("pmx-theme")).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });
});

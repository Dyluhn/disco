import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ScreenshotStrip } from "./ScreenshotStrip";

const shots = [".pmx/screenshots/0001-navigate.png", ".pmx/screenshots/0002-navigate.png"];

describe("ScreenshotStrip", () => {
  it("names each screenshot control, exposes selection and supports keyboard selection", async () => {
    const user = userEvent.setup();
    const onPick = vi.fn();
    render(<ScreenshotStrip shots={shots} hero={shots[1]} cid="conversation-a" onPick={onPick} />);
    const first = screen.getByRole("button", { name: "View screenshot 1" });
    const second = screen.getByRole("button", { name: "View screenshot 2" });
    expect(first).toHaveAttribute("aria-pressed", "false");
    expect(second).toHaveAttribute("aria-pressed", "true");
    await user.tab();
    expect(first).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(onPick).toHaveBeenLastCalledWith(shots[0]);
    await user.tab();
    expect(second).toHaveFocus();
    await user.keyboard(" ");
    expect(onPick).toHaveBeenLastCalledWith(shots[1]);
  });

  it("keeps a failed thumbnail named, visible and focused without fabricating an image", () => {
    const onPick = vi.fn();
    render(<ScreenshotStrip shots={shots} hero={shots[1]} cid="conversation-a" onPick={onPick} />);
    const first = screen.getByRole("button", { name: "View screenshot 1" });
    first.focus();
    fireEvent.error(first.querySelector("img")!);
    expect(screen.getByRole("button", { name: "View screenshot 1 (thumbnail unavailable)" })).toBe(first);
    expect(within(first).getByText("Thumbnail unavailable")).toBeVisible();
    expect(first.querySelector("img")).toBeNull();
    expect(first).toHaveFocus();
    expect(screen.getByRole("button", { name: "View screenshot 2" }).querySelector("img")).not.toBeNull();
    expect(onPick).not.toHaveBeenCalled();
  });

  it("retains failed historical thumbnails on later observations without automatic retry or path rewriting", () => {
    const onPick = vi.fn();
    const view = render(<ScreenshotStrip shots={shots} hero={shots[1]} cid="conversation-a" onPick={onPick} />);
    const first = screen.getByRole("button", { name: "View screenshot 1" });
    fireEvent.error(first.querySelector("img")!);
    // Older histories can contain aliased paths. Keep those entries; never
    // silently replace a failed old frame with newer bytes from the same path.
    view.rerender(<ScreenshotStrip shots={[...shots, shots[0]]} hero={shots[0]} cid="conversation-a" onPick={onPick} />);
    expect(screen.getAllByRole("button")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "View screenshot 1 (thumbnail unavailable)" })).toBe(first);
    expect(first.querySelector("img")).toBeNull();
    expect(screen.getByRole("button", { name: "View screenshot 3" }).querySelector("img")).toHaveAttribute(
      "src", `/conversations/conversation-a/workspace/${shots[0]}`,
    );
    expect(onPick).not.toHaveBeenCalled();
  });

  it("does not carry another conversation's failed image state into the same relative path", () => {
    const onPick = vi.fn();
    const view = render(<ScreenshotStrip shots={shots} hero={shots[1]} cid="conversation-a" onPick={onPick} />);
    fireEvent.error(screen.getByRole("button", { name: "View screenshot 1" }).querySelector("img")!);
    view.rerender(<ScreenshotStrip shots={shots} hero={shots[1]} cid="conversation-b" onPick={onPick} />);
    expect(screen.getByRole("button", { name: "View screenshot 1" }).querySelector("img")).toHaveAttribute(
      "src", `/conversations/conversation-b/workspace/${shots[0]}`,
    );
    expect(screen.queryByText("Thumbnail unavailable")).not.toBeInTheDocument();
  });
});

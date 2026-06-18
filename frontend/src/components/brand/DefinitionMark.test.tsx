/**
 * Tests for DefinitionMark and Wordmark brand components.
 *
 * Covers:
 *  1. DefinitionMark renders headword + gloss + root + accent tick at all scales
 *  2. DefinitionMark with branded=false returns null (neutral mode suppression)
 *  3. Wordmark renders "Disco" + dot
 *  4. Wordmark with branded=false renders dot in text-muted (no accent)
 *  5. Scale-to-fontSize mapping (masthead > colophon > footer implied by classes)
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DefinitionMark } from "./DefinitionMark";
import { Wordmark } from "./Wordmark";

describe("DefinitionMark", () => {
  it("renders headword 'disco'", () => {
    render(<DefinitionMark />);
    expect(screen.getByText("disco")).toBeInTheDocument();
  });

  it("renders the Latin gloss", () => {
    render(<DefinitionMark />);
    // Gloss text content
    expect(
      screen.getByText(/I learn.*acquainted/i, { exact: false }),
    ).toBeInTheDocument();
  });

  it("renders the root etymology (discere)", () => {
    render(<DefinitionMark />);
    expect(screen.getByText(/discere/i)).toBeInTheDocument();
  });

  it("renders POS 'Latin' and 'verb'", () => {
    render(<DefinitionMark />);
    expect(screen.getByText(/Latin/)).toBeInTheDocument();
    expect(screen.getByText(/verb/)).toBeInTheDocument();
  });

  it("renders at masthead scale without crashing", () => {
    const { container } = render(<DefinitionMark scale="masthead" />);
    expect(container.firstChild).toBeTruthy();
  });

  it("renders at colophon scale (default)", () => {
    const { container } = render(<DefinitionMark />);
    expect(container.firstChild).toBeTruthy();
    // default is colophon = text-[10pt]
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("10pt");
  });

  it("renders at footer scale without crashing", () => {
    const { container } = render(<DefinitionMark scale="footer" />);
    expect(container.firstChild).toBeTruthy();
  });

  it("returns null when branded=false (neutral suppression)", () => {
    const { container } = render(<DefinitionMark branded={false} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders when branded=true (explicit)", () => {
    const { container } = render(<DefinitionMark branded={true} />);
    expect(container.firstChild).toBeTruthy();
  });

  it("applies custom className", () => {
    const { container } = render(<DefinitionMark className="my-custom" />);
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("my-custom");
  });

  it("renders the accent rule tick (<i> element)", () => {
    const { container } = render(<DefinitionMark />);
    // The accent tick is a bare <i aria-hidden> element inside the .rule div
    const tick = container.querySelector("i[aria-hidden]");
    expect(tick).not.toBeNull();
  });

  it("three scales map to different font-size classes", () => {
    const { container: m } = render(<DefinitionMark scale="masthead" />);
    const { container: c } = render(<DefinitionMark scale="colophon" />);
    const { container: f } = render(<DefinitionMark scale="footer" />);

    const mastClass = (m.firstChild as HTMLElement).className;
    const colClass = (c.firstChild as HTMLElement).className;
    const footClass = (f.firstChild as HTMLElement).className;

    // All three should be distinct classes
    expect(mastClass).not.toEqual(colClass);
    expect(colClass).not.toEqual(footClass);
    expect(mastClass).not.toEqual(footClass);
  });
});

describe("Wordmark", () => {
  it("renders 'Disco' text", () => {
    render(<Wordmark />);
    // The wordmark splits "Disco" and "." into separate nodes — find the text
    expect(screen.getByText(/Disco/)).toBeInTheDocument();
  });

  it("renders the dot '.'", () => {
    render(<Wordmark />);
    expect(screen.getByText(".")).toBeInTheDocument();
  });

  it("uses --color-accent for the dot when branded=true", () => {
    render(<Wordmark branded={true} />);
    const dot = screen.getByText(".");
    expect(dot).toHaveStyle({ color: "var(--color-accent)" });
  });

  it("uses --color-text-muted for the dot when branded=false", () => {
    render(<Wordmark branded={false} />);
    const dot = screen.getByText(".");
    expect(dot).toHaveStyle({ color: "var(--color-text-muted)" });
  });

  it("applies custom className", () => {
    const { container } = render(<Wordmark className="text-xl" />);
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("text-xl");
  });

  it("renders without crashing when no props passed", () => {
    const { container } = render(<Wordmark />);
    expect(container.firstChild).toBeTruthy();
  });
});

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppCard } from "@/components/build/AppCard";
import { TweakPanel, type TweakFieldView } from "@/components/build/TweakPanel";

const FIELDS: TweakFieldView[] = [
  { key: "lead.phone", label: "Include phone", editor: "boolean" },
  { key: "brand.tone", label: "Tone", editor: "enum", options: ["calm", "bold"] },
  { key: "hero.count", label: "CTAs", editor: "int", min: 1, max: 5, step: 1 },
  { key: "brand.accent", label: "Accent", editor: "palette", colors: ["#0a84ff", "#e2725b"] },
  { key: "brand.bg", label: "Background", editor: "color" },
  { key: "hero.headline", label: "Headline", editor: "text", maxLength: 80 },
];

function renderPanel(overrides?: { fields?: TweakFieldView[]; values?: Record<string, unknown> }) {
  const onTweak = vi.fn();
  const r = render(
    <TweakPanel
      fields={overrides?.fields ?? FIELDS}
      values={(overrides?.values ?? {}) as never}
      onTweak={onTweak}
    />,
  );
  return { onTweak, ...r };
}

describe("TweakPanel", () => {
  it("boolean → checkbox emits the toggled bool", () => {
    const { onTweak } = renderPanel({ fields: [FIELDS[0]], values: { "lead.phone": false } });
    fireEvent.click(screen.getByLabelText("Include phone"));
    expect(onTweak).toHaveBeenCalledWith("lead.phone", true);
  });

  it("enum → select emits the chosen option + reflects value", () => {
    const { onTweak } = renderPanel({ fields: [FIELDS[1]], values: { "brand.tone": "calm" } });
    const sel = screen.getByLabelText("Tone") as HTMLSelectElement;
    expect(sel.value).toBe("calm");
    fireEvent.change(sel, { target: { value: "bold" } });
    expect(onTweak).toHaveBeenCalledWith("brand.tone", "bold");
  });

  it("int → range slider emits a number", () => {
    const { onTweak } = renderPanel({ fields: [FIELDS[2]], values: { "hero.count": 2 } });
    const slider = screen.getByLabelText("CTAs") as HTMLInputElement;
    expect(slider.value).toBe("2");
    fireEvent.change(slider, { target: { value: "4" } });
    expect(onTweak).toHaveBeenCalledWith("hero.count", 4);
  });

  it("palette → swatch buttons emit the color + mark the selected swatch", () => {
    const { onTweak } = renderPanel({ fields: [FIELDS[3]], values: { "brand.accent": "#0a84ff" } });
    const group = screen.getByRole("group", { name: "Accent" });
    const active = within(group).getByLabelText("#0a84ff");
    expect(active).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(group).getByLabelText("#e2725b"));
    expect(onTweak).toHaveBeenCalledWith("brand.accent", "#e2725b");
  });

  it("color → native picker emits hex (queried by label, not role)", () => {
    const { onTweak } = renderPanel({ fields: [FIELDS[4]], values: { "brand.bg": "#112233" } });
    const picker = screen.getByLabelText("Background") as HTMLInputElement;
    expect(picker.type).toBe("color");
    fireEvent.change(picker, { target: { value: "#445566" } });
    expect(onTweak).toHaveBeenCalledWith("brand.bg", "#445566");
  });

  it("text → ONE bounded input with maxLength, no textarea/contenteditable", () => {
    const { onTweak, container } = renderPanel({ fields: [FIELDS[5]], values: {} });
    const input = screen.getByLabelText("Headline") as HTMLInputElement;
    expect(input.maxLength).toBe(80);
    fireEvent.change(input, { target: { value: "Hi" } });
    expect(onTweak).toHaveBeenCalledWith("hero.headline", "Hi");
    expect(container.querySelectorAll("textarea, [contenteditable]")).toHaveLength(0);
  });

  it("no freeform soup: boolean/enum/palette/color rows emit NO stray text input/textarea", () => {
    const noText = FIELDS.filter((f) => f.editor !== "text");
    const { container } = renderPanel({ fields: noText, values: {} });
    expect(container.querySelectorAll('input[type="text"], textarea, [contenteditable]')).toHaveLength(0);
  });

  it("malformed known editor (enum w/o options) fails closed to an unsupported note", () => {
    const { container } = renderPanel({ fields: [{ key: "x.y", label: "Bad", editor: "enum" }], values: {} });
    expect(screen.getByText("unsupported control")).toBeInTheDocument();
    expect(container.querySelector("select, input")).toBeNull();
  });

  it("palette w/o colors + numeric w/o min/max/step also fail closed", () => {
    for (const bad of [
      { key: "p", label: "P", editor: "palette" as const },
      { key: "n", label: "N", editor: "int" as const, min: 0 },
    ]) {
      const { container } = renderPanel({ fields: [bad], values: {} });
      expect(within(container).getByText("unsupported control")).toBeInTheDocument();
      expect(container.querySelector("input, select")).toBeNull();
    }
  });

  it("unknown editor → unsupported note, no input", () => {
    const { container } = renderPanel({
      fields: [{ key: "z", label: "Z", editor: "spinner" as unknown as TweakFieldView["editor"] }],
      values: {},
    });
    expect(screen.getByText("unsupported control")).toBeInTheDocument();
    expect(container.querySelector("input, select")).toBeNull();
  });

  it("carries exact control metadata (data-disco-control + data-tweak-key), not P8C target attrs", () => {
    const { container } = renderPanel({ fields: [FIELDS[0]], values: {} });
    expect(container.querySelector('[data-disco-control="build.tweak.lead.phone"]')).not.toBeNull();
    expect(container.querySelector('[data-tweak-key="lead.phone"]')).not.toBeNull();
    expect(container.querySelector("[data-disco-field], [data-disco-section]")).toBeNull();
  });

  it("reflects checkbox/color/text values from props", () => {
    renderPanel({
      fields: [FIELDS[0], FIELDS[4], FIELDS[5]],
      values: { "lead.phone": true, "brand.bg": "#112233", "hero.headline": "Existing" },
    });
    expect((screen.getByLabelText("Include phone") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("Background") as HTMLInputElement).value).toBe("#112233");
    expect((screen.getByLabelText("Headline") as HTMLInputElement).value).toBe("Existing");
  });

  it("range defaults to min when no value is supplied", () => {
    renderPanel({ fields: [FIELDS[2]], values: {} });
    expect((screen.getByLabelText("CTAs") as HTMLInputElement).value).toBe("1"); // min
  });

  it("numeric with a null bound fails closed (runtime JSON null, not just undefined)", () => {
    const { container } = renderPanel({
      fields: [{ key: "n", label: "N", editor: "int", min: 0, max: null as unknown as number, step: 1 }],
      values: {},
    });
    expect(screen.getByText("unsupported control")).toBeInTheDocument();
    expect(container.querySelector("input")).toBeNull();
  });
});

describe("AppCard", () => {
  it("renders the title + the mounted TweakPanel", () => {
    render(<AppCard title="Acme" fields={[FIELDS[0]]} values={{}} onTweak={vi.fn()} />);
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.getByLabelText("Include phone")).toBeInTheDocument();
  });

  it("shows an empty-state when there are no tweaks (no stub controls)", () => {
    render(<AppCard title="Acme" fields={[]} values={{}} onTweak={vi.fn()} />);
    expect(screen.getByText(/no owner controls/i)).toBeInTheDocument();
  });

  it("passes onTweak through to the mounted panel (no swallowing)", () => {
    const onTweak = vi.fn();
    render(<AppCard title="Acme" fields={[FIELDS[0]]} values={{ "lead.phone": false }} onTweak={onTweak} />);
    fireEvent.click(screen.getByLabelText("Include phone"));
    expect(onTweak).toHaveBeenCalledWith("lead.phone", true);
  });
});

import { generateNonce } from "@/lib/selectionBridge";

export interface ElementMentionRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface ElementMentionPayload {
  domPath: string[];
  reactPath?: string[];
  screenLabel: string | null;
  text: string;
  rect: ElementMentionRect;
  href?: string;
  src?: string;
}

export interface ElementMentionMessage {
  type: "disco-element-mention";
  nonce: string;
  payload: ElementMentionPayload;
}

export interface ElementMentionArmCommand {
  type: "disco-element-mention:arm";
  nonce: string;
}

export interface ElementMentionDisarmCommand {
  type: "disco-element-mention:disarm";
  nonce: string;
}

export type ElementMentionCommand = ElementMentionArmCommand | ElementMentionDisarmCommand;

export function makeElementMentionNonce(): string {
  return generateNonce();
}

export function makeElementMentionArmCommand(nonce: string): ElementMentionArmCommand {
  return { type: "disco-element-mention:arm", nonce };
}

export function makeElementMentionDisarmCommand(nonce: string): ElementMentionDisarmCommand {
  return { type: "disco-element-mention:disarm", nonce };
}

export function sendElementMentionCommand(
  iframe: HTMLIFrameElement,
  payload: ElementMentionCommand,
): void {
  iframe.contentWindow?.postMessage(payload, "*");
}

export function parseElementMentionMessage(
  event: MessageEvent,
  allowedOrigin: string,
  nonce: string,
): ElementMentionPayload | null {
  if (!allowedOrigin || event.origin !== allowedOrigin) return null;
  const d = event.data as unknown;
  if (!d || typeof d !== "object") return null;
  const frame = d as Record<string, unknown>;
  if (frame.type !== "disco-element-mention") return null;
  if (frame.nonce !== nonce) return null;
  return parsePayload(frame.payload);
}

function parsePayload(raw: unknown): ElementMentionPayload | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const domPath = parsePath(o.domPath);
  if (!domPath) return null;
  const reactPath = o.reactPath === undefined ? undefined : parsePath(o.reactPath);
  if (o.reactPath !== undefined && !reactPath) return null;
  const screenLabel = o.screenLabel;
  if (!(screenLabel === null || typeof screenLabel === "string")) return null;
  if (typeof o.text !== "string") return null;
  if (!isRect(o.rect)) return null;
  if (o.href !== undefined && typeof o.href !== "string") return null;
  if (o.src !== undefined && typeof o.src !== "string") return null;
  return {
    domPath,
    ...(reactPath ? { reactPath } : {}),
    screenLabel: screenLabel === null ? null : cleanLine(screenLabel, 120),
    text: cleanLine(o.text, 120),
    rect: o.rect,
    ...(typeof o.href === "string" ? { href: cleanLine(o.href, 500) } : {}),
    ...(typeof o.src === "string" ? { src: cleanLine(o.src, 500) } : {}),
  };
}

function parsePath(raw: unknown): string[] | null {
  if (!Array.isArray(raw)) return null;
  if (raw.length === 0 || raw.length > 8) return null;
  const out: string[] = [];
  for (const item of raw) {
    if (typeof item !== "string") return null;
    const cleaned = cleanLine(item, 140);
    if (!cleaned) return null;
    out.push(cleaned);
  }
  return out;
}

function isRect(raw: unknown): raw is ElementMentionRect {
  if (!raw || typeof raw !== "object") return false;
  const o = raw as Record<string, unknown>;
  return (
    finiteNumber(o.x) &&
    finiteNumber(o.y) &&
    finiteNumber(o.w) &&
    finiteNumber(o.h)
  );
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function cleanLine(value: string, limit = 500): string {
  return value.replace(/\s+/g, " ").trim().slice(0, limit);
}

function blockValue(value: string): string {
  return cleanLine(value).replace(/</g, "‹").replace(/>/g, "›");
}

export function serializeElementMention(payload: ElementMentionPayload): string {
  const lines = [
    "<mentioned-element>",
    `dom: ${payload.domPath.map(blockValue).join(" > ")}`,
  ];
  if (payload.reactPath?.length) {
    lines.push(`react: ${payload.reactPath.map(blockValue).join(" > ")}`);
  }
  lines.push(`screen: ${payload.screenLabel ? blockValue(payload.screenLabel) : "null"}`);
  lines.push(`text: ${payload.text ? blockValue(payload.text) : ""}`);
  lines.push(
    `rect: x=${payload.rect.x} y=${payload.rect.y} w=${payload.rect.w} h=${payload.rect.h}`,
  );
  if (payload.href) lines.push(`href: ${blockValue(payload.href)}`);
  if (payload.src) lines.push(`src: ${blockValue(payload.src)}`);
  lines.push("</mentioned-element>");
  return lines.join("\n");
}

export function prependElementMention(
  text: string,
  payload: ElementMentionPayload | null,
): string {
  const trimmed = text.trim();
  if (!payload) return trimmed;
  return `${serializeElementMention(payload)}\n${trimmed}`;
}

export function elementMentionLabel(payload: ElementMentionPayload): string {
  const tag = payload.domPath[0]?.split(/[.#]/, 1)[0] || "element";
  const text = payload.text ? ` - ${payload.text}` : "";
  return `Element: <${tag}>${text}`;
}

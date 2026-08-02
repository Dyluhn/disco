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

export interface ParsedElementMention {
  tag: string;
  text: string;
  screen: string | null;
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

/** A per-field validation result: `valid: true` carries the parsed `value` (when the
 * field produces one); `valid: false` means the payload must be rejected. */
interface FieldResult<T> {
  valid: boolean;
  value?: T;
}

function extractReactPath(o: Record<string, unknown>): FieldResult<string[]> {
  if (o.reactPath === undefined) return { valid: true };
  const reactPath = parsePath(o.reactPath);
  if (!reactPath) return { valid: false };
  return { valid: true, value: reactPath };
}

function extractScreenLabel(o: Record<string, unknown>): FieldResult<string | null> {
  const screenLabel = o.screenLabel;
  if (!(screenLabel === null || typeof screenLabel === "string")) return { valid: false };
  return { valid: true, value: screenLabel };
}

function isValidHrefSrc(o: Record<string, unknown>): boolean {
  if (o.href !== undefined && typeof o.href !== "string") return false;
  if (o.src !== undefined && typeof o.src !== "string") return false;
  return true;
}

function buildElementMentionPayload(
  domPath: string[],
  reactPath: string[] | undefined,
  screenLabel: string | null,
  text: string,
  rect: ElementMentionRect,
  href: string | undefined,
  src: string | undefined,
): ElementMentionPayload {
  return {
    domPath,
    ...(reactPath ? { reactPath } : {}),
    screenLabel: screenLabel === null ? null : cleanLine(screenLabel, 120),
    text: cleanLine(text, 120),
    rect,
    ...(href !== undefined ? { href: cleanLine(href, 500) } : {}),
    ...(src !== undefined ? { src: cleanLine(src, 500) } : {}),
  };
}

function parsePayload(raw: unknown): ElementMentionPayload | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const domPath = parsePath(o.domPath);
  if (!domPath) return null;
  const reactPathResult = extractReactPath(o);
  if (!reactPathResult.valid) return null;
  const screenLabelResult = extractScreenLabel(o);
  if (!screenLabelResult.valid) return null;
  if (typeof o.text !== "string") return null;
  if (!isRect(o.rect)) return null;
  if (!isValidHrefSrc(o)) return null;
  return buildElementMentionPayload(
    domPath,
    reactPathResult.value,
    screenLabelResult.value ?? null,
    o.text,
    o.rect,
    typeof o.href === "string" ? o.href : undefined,
    typeof o.src === "string" ? o.src : undefined,
  );
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

function parseBlockValue(value: string): string {
  return value.trim().replace(/‹/g, "<").replace(/›/g, ">");
}

function parseMentionBlock(block: string): ParsedElementMention {
  const lines = block.replace(/^\r?\n/, "").split(/\r?\n/);
  const dom = parseBlockValue(
    lines.find((line) => line.startsWith("dom:"))?.slice("dom:".length) ?? "",
  );
  const firstPath = dom.split(" > ", 1)[0] ?? "";
  const tag = firstPath.split(/[.#]/, 1)[0] || "element";
  const text = parseBlockValue(
    lines.find((line) => line.startsWith("text:"))?.slice("text:".length) ?? "",
  );
  const screenValue = parseBlockValue(
    lines.find((line) => line.startsWith("screen:"))?.slice("screen:".length) ?? "null",
  );
  return { tag, text, screen: screenValue === "null" ? null : screenValue };
}

export function stripElementMention(message: string): {
  clean: string;
  mention: ParsedElementMention | null;
} {
  const open = "<mentioned-element>";
  const close = "</mentioned-element>";
  const start = message.indexOf(open);
  if (start < 0) return { clean: message.trim(), mention: null };
  const bodyStart = start + open.length;
  const end = message.indexOf(close, bodyStart);
  if (end < 0) {
    return { clean: "", mention: parseMentionBlock(message.slice(bodyStart)) };
  }
  const body = message.slice(bodyStart, end);
  const afterStart = end + close.length;
  const clean = `${message.slice(0, start)}${message.slice(afterStart)}`.trim();
  return { clean, mention: parseMentionBlock(body) };
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

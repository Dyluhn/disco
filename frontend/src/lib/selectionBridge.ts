/**
 * Selection bridge — versioned postMessage transport between host and preview iframes.
 *
 * Security model
 * ─────────────
 * Every frame carries `{v:1, channel:"disco-select", nonce}`.
 *
 * • srcdoc iframes (sandboxed, no allow-same-origin): the browser reports
 *   `event.origin === "null"` (the string). Origin is structurally unverifiable,
 *   so we accept the string "null" and rely on the per-session nonce as the sole
 *   security gate.  The nonce is generated on each arm() call and sent to the
 *   frame via the arm command; a forged postMessage from a co-sandboxed frame
 *   would have to guess the nonce.
 *
 * • live-proxy iframes (allow-same-origin): `event.origin` is the preview host
 *   URL. Both origin AND nonce are validated.
 *
 * Any frame failing validation is silently dropped.
 *
 * @module selectionBridge
 */

// ─── Public types ────────────────────────────────────────────────────────────

/** A deck element reference (ref fields are schema-frozen by Track C / C4). */
export type DeckRef = {
  kind: "deck";
  slide_id: string;
  element_id: string;
};

/** A source-file reference resolved from a `data-oid` attribute. */
export type SourceRef = {
  kind: "source";
  oid: string;
  file: string;
  line: number;
};

/** An AppKit `data-disco-*` semantic anchor (P8). `field_id` ⇒ a single field of a
 *  section; `collection_id`+`index` ⇒ the Nth item; section-only ⇒ the whole section.
 *  Mirrors the Python `SemanticSelectionRef` (core/selection_edit.py). */
export type SemanticRef = {
  kind: "semantic";
  section_id: string;
  field_id?: string;
  collection_id?: string;
  index?: number;
  screen_label?: string;
};

export type SelectionRef = DeckRef | SourceRef | SemanticRef;

/** Common header stamped on every bridge frame (host→iframe and iframe→host). */
interface BridgeFrame {
  readonly v: 1;
  readonly channel: "disco-select";
  readonly nonce: string;
}

/**
 * A user-selected element sent from the in-frame agent to the host.
 *
 * This is the canonical envelope shape; the Python `deck_patch` tool input
 * mirrors it (§4.4).
 */
export interface SelectionEnvelope extends BridgeFrame {
  readonly type: "disco:selection";
  /** Resolver-tagged target: DeckRef for slides, SourceRef for app source. */
  readonly selection_ref: SelectionRef;
  /** Human-readable description, e.g. "h2.title — “Q3 Revenue”". */
  readonly human_label: string;
  /** Bounding rect in iframe viewport coordinates. */
  readonly rect: { x: number; y: number; width: number; height: number };
  /** dataURL crop captured host-side — only present when §2 vision is active. */
  readonly screenshot_crop?: string;
  /** User-supplied edit instruction, filled when submitted via the host UI. */
  readonly edit_instruction?: string;
}

/** In-frame hover notification (optional — used for the host hover-ring). */
export interface HoverEnvelope extends BridgeFrame {
  readonly type: "disco:hover";
  /** Bounding rect in iframe viewport coordinates. */
  readonly rect: { x: number; y: number; width: number; height: number };
}

/** Host → iframe: arm selection mode with this session's nonce. */
export interface ArmCommand extends BridgeFrame {
  readonly type: "disco:overlay:arm";
}

/** Host → iframe: disarm (remove event listeners + ring). */
export interface DisarmCommand extends BridgeFrame {
  readonly type: "disco:overlay:disarm";
}

/** Host → iframe: re-target selection to the current element's parentElement. */
export interface WalkUpCommand extends BridgeFrame {
  readonly type: "disco:overlay:walkup";
}

/** Messages that flow FROM the iframe TO the host. */
export type InFrameMessage = SelectionEnvelope | HoverEnvelope;

/** Commands that flow FROM the host TO the iframe. */
export type HostCommand = ArmCommand | DisarmCommand | WalkUpCommand;

// ─── Nonce utilities ─────────────────────────────────────────────────────────

/**
 * Generate a cryptographically random per-session nonce.
 * One nonce per arm() call; discarded on disarm.
 */
export function generateNonce(): string {
  const buf = new Uint8Array(16);
  crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}

// ─── Frame validation ─────────────────────────────────────────────────────────

/**
 * Parse and validate an incoming `MessageEvent` from a preview iframe.
 *
 * Returns the typed `InFrameMessage` on success, or `null` when any guard fails:
 * - wrong `event.origin` (must equal `allowedOrigin`)
 * - wrong `v`, `channel`, or `nonce` header fields
 * - unrecognised or malformed `type`
 *
 * @param event        - The raw `MessageEvent` from `window.addEventListener("message")`.
 * @param allowedOrigin - The exact origin string to accept.  Pass `"null"` (the
 *   string) for srcdoc iframes whose sandboxed context always reports `"null"`.
 *   Pass the proxy host URL (from `previewHostUrl`) for live-proxy iframes.
 * @param nonce        - The per-session nonce generated at arm() time.
 */
export function parseInFrameMessage(
  event: MessageEvent,
  allowedOrigin: string,
  nonce: string,
): InFrameMessage | null {
  // Guard 1: origin
  if (event.origin !== allowedOrigin) return null;

  // Guard 2: basic shape + header fields
  const d = event.data as unknown;
  if (!d || typeof d !== "object") return null;
  const frame = d as Record<string, unknown>;
  if (frame["v"] !== 1) return null;
  if (frame["channel"] !== "disco-select") return null;
  if (frame["nonce"] !== nonce) return null;

  // Guard 3: known type
  const type = frame["type"];
  if (type === "disco:selection") {
    return parseSelectionEnvelope(frame, nonce);
  }
  if (type === "disco:hover") {
    return parseHoverEnvelope(frame, nonce);
  }
  return null;
}

function parseSelectionEnvelope(
  frame: Record<string, unknown>,
  nonce: string,
): SelectionEnvelope | null {
  const ref = frame["selection_ref"];
  const label = frame["human_label"];
  const rect = frame["rect"];
  if (!isSelectionRef(ref)) return null;
  if (typeof label !== "string") return null;
  if (!isRect(rect)) return null;
  const env: SelectionEnvelope = {
    v: 1,
    channel: "disco-select",
    nonce,
    type: "disco:selection",
    selection_ref: ref,
    human_label: label,
    rect,
  };
  // Optional screenshot_crop
  if (typeof frame["screenshot_crop"] === "string") {
    return { ...env, screenshot_crop: frame["screenshot_crop"] as string };
  }
  return env;
}

function parseHoverEnvelope(
  frame: Record<string, unknown>,
  nonce: string,
): HoverEnvelope | null {
  const rect = frame["rect"];
  if (!isRect(rect)) return null;
  return {
    v: 1,
    channel: "disco-select",
    nonce,
    type: "disco:hover",
    rect,
  };
}

function isRect(
  r: unknown,
): r is { x: number; y: number; width: number; height: number } {
  if (!r || typeof r !== "object") return false;
  const o = r as Record<string, unknown>;
  return (
    typeof o["x"] === "number" &&
    typeof o["y"] === "number" &&
    typeof o["width"] === "number" &&
    typeof o["height"] === "number"
  );
}

function isSelectionRef(r: unknown): r is SelectionRef {
  if (!r || typeof r !== "object") return false;
  const o = r as Record<string, unknown>;
  if (o["kind"] === "deck") {
    return typeof o["slide_id"] === "string" && typeof o["element_id"] === "string";
  }
  if (o["kind"] === "source") {
    return (
      typeof o["oid"] === "string" &&
      typeof o["file"] === "string" &&
      typeof o["line"] === "number"
    );
  }
  if (o["kind"] === "semantic") {
    // section_id required; each optional field must be the right type WHEN present.
    return (
      typeof o["section_id"] === "string" &&
      _optStr(o["field_id"]) &&
      _optStr(o["collection_id"]) &&
      _optStr(o["screen_label"]) &&
      (o["index"] === undefined || typeof o["index"] === "number")
    );
  }
  return false;
}

function _optStr(v: unknown): boolean {
  return v === undefined || typeof v === "string";
}

// ─── Host → iframe transport ──────────────────────────────────────────────────

/**
 * Send a command from the host to a preview iframe.
 *
 * Posts to `iframe.contentWindow` with `targetOrigin = "*"`.  This is
 * intentional: srcdoc iframes report origin `"null"` which cannot be used as
 * a `targetOrigin`, and live-proxy frames (also served under `localhost`)
 * similarly require `"*"`.  The nonce embedded in every command guards the
 * in-frame handler against replay from unrelated postMessage senders.
 */
export function sendToFrame(iframe: HTMLIFrameElement, payload: HostCommand): void {
  iframe.contentWindow?.postMessage(payload, "*");
}

/** Build the arm command for this session's nonce. */
export function makeArmCommand(nonce: string): ArmCommand {
  return { v: 1, channel: "disco-select", nonce, type: "disco:overlay:arm" };
}

/** Build the disarm command for this session's nonce. */
export function makeDisarmCommand(nonce: string): DisarmCommand {
  return { v: 1, channel: "disco-select", nonce, type: "disco:overlay:disarm" };
}

/** Build the walk-up command for this session's nonce. */
export function makeWalkUpCommand(nonce: string): WalkUpCommand {
  return { v: 1, channel: "disco-select", nonce, type: "disco:overlay:walkup" };
}

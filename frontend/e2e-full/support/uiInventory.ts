/**
 * uiInventory — enumerate, classify, and track UI controls on a Playwright page.
 *
 * `enumerateControls(page)` queries every interactive element and classifies
 * each into one of five kinds:
 *
 *   backend-command  Has data-disco-control OR matches known action names →
 *                    causes an HTTP/WS call.  ONLY these require four-truth
 *                    backend evidence; the coverage gate fires only for them.
 *   navigation       <a href="..."> — routes the user elsewhere.
 *   form-input       input / textarea / select / [role=textbox] — captures data.
 *   local-ui-only    Visible interactive element with no known backend effect
 *                    (tabs, toggles, disclosure triggers, etc.).
 *   disabled         Has [disabled] or [aria-disabled=true]; skipped in coverage.
 *
 * `HitMap` tracks which controls were seen vs. clicked.
 * `assertCoverage(allowlist)` fails if any enabled+visible backend-command
 * control was discovered but never clicked (unless allowlisted with a reason).
 *
 * evidence-harness-campaign.md W12
 */

import type { Page } from "@playwright/test";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Classification of a UI control. */
export type ControlKind =
  | "backend-command"
  | "navigation"
  | "form-input"
  | "local-ui-only"
  | "disabled";

/** One enumerated control on a surface. */
export interface Control {
  /** URL path at enumeration time, e.g. "/build/abc123". */
  route: string;
  /** Surface derived from the route, e.g. "build", "home", "settings". */
  surface: string;
  /** ARIA role attribute or HTML tag name. */
  role: string;
  /** Best accessible name: aria-label → text content → empty. */
  name: string;
  /** Whether the control is interactive (not disabled). */
  enabled: boolean;
  /** Whether the control is currently visible in the viewport. */
  visible: boolean;
  /** data-testid attribute, or null. */
  testId: string | null;
  /**
   * Stable identifier: data-disco-control → data-testid → kebab(name+role).
   * Used as the key in HitMap.
   */
  controlId: string;
  kind: ControlKind;
}

// ---------------------------------------------------------------------------
// Classification helpers
// ---------------------------------------------------------------------------

/**
 * Heuristic action-name patterns that strongly imply a backend call.
 * Matched against the lowercased text content / aria-label.
 */
const BACKEND_COMMAND_PATTERNS: RegExp[] = [
  /\bsubmit\b/,
  /\bsend\b/,
  /\bsearch\b/,
  /\bapprove\b/,
  /\breject\b/,
  /\bcancel\b/,
  /\bdelete\b/,
  /\bremove\b/,
  /\bsave\b/,
  /\bcreate\b/,
  /\bnew\b/,
  /\bstart\b/,
  /\bstop\b/,
  /\bpause\b/,
  /\bresume\b/,
  /\bconfirm\b/,
  /\brun\b/,
  /\bexport\b/,
  /\bdownload\b/,
  /\bupload\b/,
  /\bimport\b/,
  /\bgenerate\b/,
  /\bbuild\b/,
  /\bdeploy\b/,
  /\bpublish\b/,
  /\bconnect\b/,
  /\bdisconnect\b/,
  /\brefresh\b/,
  /\breload\b/,
  /\bkill\b/,
  /\breplan\b/,
  /\bshare\b/,
  /\brevoke\b/,
];

/**
 * Classify a raw element into a {@link ControlKind}.
 *
 * Priority:
 *  1. disabled / aria-disabled → "disabled"
 *  2. has data-disco-control → "backend-command"
 *  3. <a href="..."> → "navigation"
 *  4. input / textarea / select / role=textbox → "form-input"
 *  5. role=tab / role=radio / role=switch → "local-ui-only" (state-only)
 *  6. name matches BACKEND_COMMAND_PATTERNS → "backend-command"
 *  7. fallback → "local-ui-only"
 */
function classifyKind(
  tagName: string,
  role: string,
  name: string,
  discoControl: string | null,
  href: string | null,
  isDisabled: boolean,
): ControlKind {
  if (isDisabled) return "disabled";
  if (discoControl !== null) return "backend-command";
  if (tagName === "a" && href !== null) return "navigation";
  if (
    tagName === "input" ||
    tagName === "textarea" ||
    tagName === "select" ||
    role === "textbox"
  ) {
    return "form-input";
  }
  if (role === "tab" || role === "radio" || role === "switch") {
    return "local-ui-only";
  }
  const lower = name.toLowerCase();
  if (BACKEND_COMMAND_PATTERNS.some((p) => p.test(lower))) {
    return "backend-command";
  }
  return "local-ui-only";
}

/** Derive a surface name from a URL path. */
function surfaceFromRoute(route: string): string {
  try {
    const pathname = new URL(route).pathname;
    const segment = pathname.split("/").filter(Boolean)[0];
    return segment ?? "home";
  } catch {
    return "unknown";
  }
}

/** Build a stable controlId from the available attributes. */
function makeControlId(
  discoControl: string | null,
  testId: string | null,
  name: string,
  role: string,
): string {
  if (discoControl !== null && discoControl.length > 0) return `disco:${discoControl}`;
  if (testId !== null && testId.length > 0) return `tid:${testId}`;
  const slug = `${name}-${role}`
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 60);
  return `gen:${slug || "unnamed"}`;
}

// ---------------------------------------------------------------------------
// Raw element shape returned from page.evaluate / locator.evaluate
// ---------------------------------------------------------------------------

interface RawAttrs {
  tagName: string;
  roleAttr: string | null;
  ariaLabel: string | null;
  dataTestId: string | null;
  discoControl: string | null;
  disabledAttr: boolean;
  ariaDisabled: string | null;
  href: string | null;
}

// ---------------------------------------------------------------------------
// enumerateControls
// ---------------------------------------------------------------------------

/**
 * CSS selector covering all interactive elements the harness cares about.
 * Deduplication is handled after enumeration via controlId.
 */
const CONTROL_SELECTOR = [
  "button",
  "a[href]",
  "input:not([type='hidden'])",
  "textarea",
  "select",
  "[role='button']",
  "[role='switch']",
  "[role='radio']",
  "[role='tab']",
  "[role='menuitem']",
  "[role='textbox']",
  "[data-testid]",
  "[data-disco-control]",
].join(", ");

/**
 * Enumerate all interactive controls on `page` and classify each.
 *
 * Skips duplicate controlIds (keeps the first occurrence).
 * Does NOT filter by visibility here — callers should check `ctrl.visible`.
 */
export async function enumerateControls(page: Page): Promise<Control[]> {
  const route = page.url();
  const surface = surfaceFromRoute(route);

  const locators = await page.locator(CONTROL_SELECTOR).all();

  const seen = new Set<string>();
  const controls: Control[] = [];

  for (const loc of locators) {
    // Single evaluate call per element to minimise round-trips
    const attrs: RawAttrs = await loc.evaluate((rawEl) => {
      // Cast through unknown to avoid needing DOM lib in tsconfig
      const el = rawEl as unknown as {
        tagName: string;
        getAttribute(name: string): string | null;
        hasAttribute(name: string): boolean;
      };
      return {
        tagName: el.tagName.toLowerCase(),
        roleAttr: el.getAttribute("role"),
        ariaLabel: el.getAttribute("aria-label"),
        dataTestId: el.getAttribute("data-testid"),
        discoControl: el.getAttribute("data-disco-control"),
        disabledAttr: el.hasAttribute("disabled"),
        ariaDisabled: el.getAttribute("aria-disabled"),
        href: el.getAttribute("href"),
      };
    });

    const isDisabled =
      attrs.disabledAttr || attrs.ariaDisabled === "true";

    const role = attrs.roleAttr ?? attrs.tagName;
    const textContent = (await loc.textContent())?.trim() ?? "";
    const name = attrs.ariaLabel ?? textContent;
    const controlId = makeControlId(
      attrs.discoControl,
      attrs.dataTestId,
      name,
      role,
    );

    // Skip duplicates (same controlId already collected)
    if (seen.has(controlId)) continue;
    seen.add(controlId);

    const visible = await loc.isVisible();

    const kind = classifyKind(
      attrs.tagName,
      role,
      name,
      attrs.discoControl,
      attrs.href,
      isDisabled,
    );

    controls.push({
      route,
      surface,
      role,
      name,
      enabled: !isDisabled,
      visible,
      testId: attrs.dataTestId,
      controlId,
      kind,
    });
  }

  return controls;
}

// ---------------------------------------------------------------------------
// HitMap
// ---------------------------------------------------------------------------

/**
 * Tracks which controls were enumerated (seen) vs. actually clicked (hit).
 *
 * Feed every `enumerateControls` result through `record()`.
 * Call `hit(controlId)` after each `discoClick`.
 * Call `assertCoverage(allowlist)` at end-of-suite to enforce the coverage gate.
 */
export class HitMap {
  private readonly _seen = new Map<string, Control>();
  private readonly _clicked = new Set<string>();

  /** Record a discovered control. Safe to call multiple times with the same id. */
  record(control: Control): void {
    if (!this._seen.has(control.controlId)) {
      this._seen.set(control.controlId, control);
    }
  }

  /** Record a set of controls (convenience for the array from enumerateControls). */
  recordAll(controls: Control[]): void {
    for (const c of controls) this.record(c);
  }

  /** Mark a control as exercised by a click. */
  hit(controlId: string): void {
    this._clicked.add(controlId);
  }

  /** All controls discovered so far. */
  get allSeen(): Control[] {
    return [...this._seen.values()];
  }

  /** All clicked control IDs. */
  get allClicked(): ReadonlySet<string> {
    return this._clicked;
  }

  /**
   * Fail if any enabled, visible `backend-command` control was discovered
   * but never clicked, unless its controlId appears in `allowlist`.
   *
   * @param allowlist  Map from controlId → reason string (human-readable).
   * @throws {Error}   Listing every un-exercised control with its surface/name.
   */
  assertCoverage(allowlist: Record<string, string> = {}): void {
    const failures: string[] = [];
    for (const [id, ctrl] of this._seen) {
      if (ctrl.kind !== "backend-command") continue;
      if (!ctrl.enabled || !ctrl.visible) continue;
      if (id in allowlist) continue;
      if (!this._clicked.has(id)) {
        failures.push(
          `  [${ctrl.surface}] ${id}  name="${ctrl.name}"  role=${ctrl.role}`,
        );
      }
    }
    if (failures.length > 0) {
      throw new Error(
        `Coverage gate: ${failures.length} backend-command control(s) discovered but never exercised:\n${failures.join("\n")}`,
      );
    }
  }
}

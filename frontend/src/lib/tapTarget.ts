/**
 * Shared mobile primitive (owned by the shell/Settings agent — see App.tsx /
 * components/settings/** ownership) for the 44×44 CSS px minimum touch target
 * the mobile spec requires.
 *
 * Below `lg` (1024px) a lot of this app's controls are sized for a dense,
 * mouse-driven desktop form (18–30px tall icon buttons, toggles, tab links).
 * That's fine with a cursor; it's not reliably tappable with a thumb. These
 * two class strings pad the box up to 44px ONLY below `lg`, so desktop stays
 * pixel-identical (nothing changes at `lg:` and up — the `lg:min-h-0`
 * / `lg:min-w-0` pair explicitly hands sizing back to the element's own
 * classes at that breakpoint).
 *
 * USAGE
 *   import { TAP_TARGET, TAP_TARGET_ICON } from "@/lib/tapTarget";
 *
 *   // A button/link that already has real content driving its width (text,
 *   // icon+text) and just needs a taller mobile hit box:
 *   <button className={cn("px-inline py-hair ...", TAP_TARGET)}>Save</button>
 *
 *   // An icon-ONLY button (no text) — also centers the icon in the new box:
 *   <button className={cn("rounded-control text-text-faint ...", TAP_TARGET_ICON)}>
 *     <Trash2 className="size-3.5" />
 *   </button>
 *
 * Do NOT use this on fixed-size decorative elements (status dots, switch
 * thumbs) where growing the box would visibly distort the control — for a
 * small control whose VISUAL size must stay fixed (e.g. a toggle switch),
 * expand the tap area with an absolutely-positioned invisible hit slop
 * instead, sized to clear neighboring controls' hit areas by at least the
 * existing gap.
 */

/** Floors width/height at 44px below `lg`; no-op at `lg:` and up. */
export const TAP_TARGET = "min-h-11 min-w-11 lg:min-h-0 lg:min-w-0";

/** TAP_TARGET plus centering, for icon-only controls whose visible icon is
 * smaller than the new tap box. */
export const TAP_TARGET_ICON = `inline-flex items-center justify-center ${TAP_TARGET}`;

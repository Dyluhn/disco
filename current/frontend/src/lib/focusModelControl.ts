/** Shared reveal helper for the driver-model notice: after a composer's
 * options panel opens, scroll its model control into view and focus it so the
 * click lands ON the config affordance the notice points at. Prefers the
 * registered model pill (Deep Research's panel leads with the depth control);
 * falls back to the panel's first control (Build's picker renders first and
 * carries no registry id). */
export function focusModelControl(panelId: string) {
  requestAnimationFrame(() => {
    const panel = document.getElementById(panelId);
    const el =
      panel?.querySelector<HTMLElement>('[data-disco-control="search.model-pill"]') ??
      panel?.querySelector<HTMLElement>("button, [role='combobox']");
    el?.scrollIntoView?.({ block: "nearest" });
    el?.focus();
  });
}

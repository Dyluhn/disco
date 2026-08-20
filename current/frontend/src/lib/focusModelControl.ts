/** Shared reveal helper for the driver-model notice: after a composer's
 * options panel opens, scroll its model control (each panel renders it first)
 * into view and focus it so the click lands ON the config affordance the
 * notice points at. */
export function focusModelControl(panelId: string) {
  requestAnimationFrame(() => {
    const el = document
      .getElementById(panelId)
      ?.querySelector<HTMLElement>("button, [role='combobox']");
    el?.scrollIntoView?.({ block: "nearest" });
    el?.focus();
  });
}

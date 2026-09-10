/**
 * Deep Research no longer exposes an iterative/non-iterative selector.
 *
 * This compatibility export remains so downstream imports do not break while
 * the product exposes exactly one adaptive path. No runtime module imports it,
 * and even a stale downstream render produces no selector.
 */
interface Props {
  value: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
}

export function IterativeToggle({ value, onChange, disabled }: Props) {
  void value;
  void onChange;
  void disabled;
  return null;
}

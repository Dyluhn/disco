import { Moon, Sun } from "lucide-react";
import { useTheme } from "@/lib/useTheme";
import { TAP_TARGET } from "@/lib/tapTarget";
import { cn } from "@/lib/cn";

export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  return (
    <button
      type="button"
      data-disco-control="shell.theme-toggle"
      data-theme={theme}
      onClick={toggle}
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
      className={cn(
        "grid size-8 shrink-0 place-items-center rounded-control border border-hairline text-text-muted transition-colors hover:text-text",
        TAP_TARGET,
      )}
    >
      {theme === "dark" ? <Sun className="size-4" aria-hidden /> : <Moon className="size-4" aria-hidden />}
    </button>
  );
}

import * as Dialog from "@radix-ui/react-dialog";
import { Search, Plus, Clock, FolderGit2, Settings, Moon, Sun } from "lucide-react";
import { useEffect, useMemo, useState, useCallback, type ComponentType } from "react";
import { useNavigate } from "react-router-dom";
import { useTheme } from "@/lib/useTheme";
import { cn } from "@/lib/cn";

interface CommandItem {
  id: string;
  label: string;
  icon: ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  action: () => void;
}

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const navigate = useNavigate();
  const { theme, toggle: toggleTheme } = useTheme();

  const commands: CommandItem[] = useMemo(() => [
    { id: "new", label: "New build", icon: Plus, action: () => navigate("/") },
    { id: "history", label: "History", icon: Clock, action: () => navigate("/history") },
    { id: "projects", label: "Projects", icon: FolderGit2, action: () => navigate("/projects") },
    { id: "settings", label: "Settings", icon: Settings, action: () => navigate("/settings") },
    {
      id: "theme",
      label: `Toggle theme (${theme === "dark" ? "light" : "dark"})`,
      icon: theme === "dark" ? Sun : Moon,
      action: toggleTheme,
    },
  ], [navigate, theme, toggleTheme]);

  const filteredCommands = useMemo(() => {
    return commands.filter(c => c.label.toLowerCase().includes(query.toLowerCase()));
  }, [commands, query]);

  useEffect(() => {
    setSelectedIndex(0);
  }, [query]);

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setOpen((open) => !open);
      }
    };

    document.addEventListener("keydown", down);
    return () => document.removeEventListener("keydown", down);
  }, []);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIndex((i) => (i + 1) % filteredCommands.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIndex((i) => (i - 1 + filteredCommands.length) % filteredCommands.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const command = filteredCommands[selectedIndex];
      if (command) {
        command.action();
        setOpen(false);
        setQuery("");
      }
    }
  }, [filteredCommands, selectedIndex]);

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/45 backdrop-blur-[2px]" />
        <Dialog.Content className="fixed left-1/2 top-[20%] z-50 w-full max-w-lg -translate-x-1/2 overflow-hidden rounded-control border border-hairline bg-surface-1 shadow-2xl pmx-rise focus:outline-none">
          <div className="flex items-center border-b border-hairline px-4 py-3">
            <Search className="mr-3 size-4 text-text-muted" />
            <input
              autoFocus
              className="w-full bg-transparent font-ui text-[0.95rem] text-text placeholder:text-text-faint focus:outline-none"
              placeholder="Search commands..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={handleKeyDown}
            />
          </div>
          <div className="max-h-[300px] overflow-y-auto p-2">
            {filteredCommands.length === 0 ? (
              <p className="p-4 text-center font-ui text-[0.86rem] text-text-muted">No commands found.</p>
            ) : (
              <ul className="flex flex-col gap-px">
                {filteredCommands.map((command, i) => {
                  const Icon = command.icon;
                  return (
                    <li key={command.id}>
                      <button
                        className={cn(
                          "flex w-full items-center gap-3 rounded-control px-3 py-2 text-left font-ui text-[0.86rem] transition-colors",
                          i === selectedIndex
                            ? "bg-surface-2 text-accent"
                            : "text-text-muted hover:bg-surface-2 hover:text-text"
                        )}
                        onClick={() => {
                          command.action();
                          setOpen(false);
                          setQuery("");
                        }}
                        onMouseEnter={() => setSelectedIndex(i)}
                      >
                        <Icon className="size-4 shrink-0" aria-hidden />
                        <span className="flex-1">{command.label}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

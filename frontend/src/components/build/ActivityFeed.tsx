/**
 * The Agent Activity Feed (BoD §13.4, Project-Manager tier): a plain-language, audit-log
 * narrative of what the agent did and why — NOT raw tool calls. A confidence gradient
 * drives attention: routine, high-confidence steps recede (muted); risky / pending /
 * failed steps are emphasized. The technical specifics (commands, output) live in the
 * Inspector canvas, never here.
 */

import { AlertTriangle, Check, Circle, Loader2, X } from "lucide-react";
import { cn } from "@/lib/cn";
import type { ActivityItem } from "@/lib/buildTrace";

function StatusDot({ item }: { item: ActivityItem }) {
  if (item.status === "running")
    return <Loader2 className="size-3.5 animate-spin text-text-muted" aria-label="running" />;
  if (item.status === "pending")
    return <AlertTriangle className="size-3.5 text-warn" aria-label="awaiting approval" />;
  if (item.status === "failed") return <X className="size-3.5 text-unsupported" aria-label="failed" />;
  return <Check className="size-3.5 text-supported" aria-label="done" />;
}

export function ActivityFeed({ items }: { items: ActivityItem[] }) {
  if (items.length === 0) {
    return (
      <p className="flex items-center gap-hair px-px font-ui text-[0.82rem] text-text-faint">
        <Circle className="size-3 animate-pulse" aria-hidden />
        Planning the first step…
      </p>
    );
  }
  return (
    <ol className="flex flex-col">
      {items.map((item, i) => (
        <li
          key={item.id}
          className={cn(
            "flex items-start gap-inline border-l-2 py-inline pl-body",
            item.attention ? "border-warn/60" : "border-transparent",
          )}
        >
          <span className="mt-px shrink-0">
            <StatusDot item={item} />
          </span>
          <div className="flex min-w-0 flex-col gap-px">
            <span
              className={cn(
                "font-ui text-[0.88rem] leading-snug",
                item.attention ? "font-medium text-text" : "text-text-muted",
                item.status === "failed" && "text-unsupported",
              )}
            >
              {item.label}
              {item.status === "pending" && (
                <span className="ml-inline font-ui text-[0.7rem] uppercase tracking-wide text-warn">
                  needs approval
                </span>
              )}
            </span>
            {item.detail && (
              <span className="truncate font-ui text-[0.78rem] leading-snug text-text-faint">
                {item.detail}
              </span>
            )}
          </div>
          {i === items.length - 1 && item.status === "running" && (
            <span className="ml-auto shrink-0 font-ui text-[0.7rem] uppercase tracking-wide text-text-faint">
              now
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}

import { cn } from "@/lib/cn";

export function TableView({
  columns,
  rows,
  caption,
}: {
  columns: string[];
  rows: string[][];
  caption?: string;
}) {
  return (
    <figure className="m-0">
      <div className="overflow-x-auto rounded-card border border-hairline">
        <table className="w-full table-fixed border-collapse font-ui text-[0.85rem]">
          <thead>
            <tr className="bg-surface-1">
              {columns.map((c, i) => (
                <th
                  key={i}
                  className="border-b border-hairline px-body py-inline text-left font-semibold text-text-muted"
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, ri) => (
              <tr key={ri} className="border-b border-hairline last:border-0">
                {row.map((cell, ci) => (
                  <td
                    key={ci}
                    className={cn(
                      "px-body py-inline align-top",
                      ci === 0 ? "font-medium text-text-muted" : "text-text",
                    )}
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {caption && (
        <figcaption className="mt-hair font-ui text-[0.74rem] text-text-faint">
          {caption}
        </figcaption>
      )}
    </figure>
  );
}

import { useEffect, useMemo, useRef, useState } from "react";
import { Chart, registerables } from "chart.js";
import { TableView } from "./TableView";

Chart.register(...registerables);

const COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"];

// Exported so Markdown.tsx can lift ```chart fences (deep-research report sections
// arrive as raw markdown, not structured blocks) into real inline charts.
export function ChartBlockComponent({
  chart_type,
  data,
  title,
  x_label,
  y_label,
}: {
  chart_type: string;
  data: any;
  title?: string;
  x_label?: string;
  y_label?: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [error, setError] = useState(false);

  const tableData = useMemo(() => {
    if (!data || !Array.isArray(data)) return null;
    try {
      if (chart_type === "scatter") {
        return {
          columns: ["Group", x_label || "X", y_label || "Y"],
          rows: data.map((d: any) => [String(d.group || "Default"), String(d.x), String(d.y)]),
        };
      }
      return {
        columns: [x_label || "Label", y_label || "Value"],
        rows: data.map((d: any) => [
          String(d.label || d.x || ""),
          String(d.value !== undefined ? d.value : d.y || ""),
        ]),
      };
    } catch {
      return null;
    }
  }, [data, chart_type, x_label, y_label]);

  useEffect(() => {
    if (error || !canvasRef.current || !data || !Array.isArray(data)) return;

    let chart: Chart | null = null;
    try {
      const ctx = canvasRef.current.getContext("2d");
      if (!ctx) return;

      const isScatter = chart_type === "scatter";
      const isPie = chart_type === "pie";

      chart = new Chart(ctx, {
        type: (chart_type as any) || "bar",
        data: {
          labels: isScatter ? [] : data.map((d: any) => d.label || d.x || ""),
          datasets: isScatter
            ? Array.from(new Set(data.map((d: any) => d.group || "Default"))).map((g, i) => ({
                label: String(g),
                data: data
                  .filter((d: any) => (d.group || "Default") === g)
                  .map((d: any) => ({ x: d.x, y: d.y })),
                backgroundColor: COLORS[i % COLORS.length],
              }))
            : [
                {
                  label: y_label || "Value",
                  data: data.map((d: any) => (d.value !== undefined ? d.value : d.y || 0)),
                  backgroundColor: isPie ? COLORS : COLORS[0],
                  borderColor: COLORS[0],
                  borderWidth: isPie ? 0 : 2,
                  tension: 0.1,
                },
              ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          plugins: {
            title: { display: !!title, text: title, color: "#e6edf3" },
            legend: {
              display: isPie || isScatter,
              labels: { color: "#8b949e", font: { family: "system-ui" } },
            },
            tooltip: {
              backgroundColor: "#161b22",
              titleColor: "#e6edf3",
              bodyColor: "#8b949e",
              borderColor: "#30363d",
              borderWidth: 1,
            },
          },
          scales: isPie
            ? {}
            : {
                x: {
                  title: { display: !!x_label, text: x_label, color: "#8b949e" },
                  ticks: { color: "#8b949e" },
                  grid: { color: "rgba(48, 54, 61, 0.5)" },
                },
                y: {
                  title: { display: !!y_label, text: y_label, color: "#8b949e" },
                  ticks: { color: "#8b949e" },
                  grid: { color: "rgba(48, 54, 61, 0.5)" },
                  beginAtZero: true,
                },
              },
        },
      });
    } catch (e) {
      console.error("Chart.js init failed", e);
      setError(true);
    }
    return () => chart?.destroy();
  }, [chart_type, data, title, x_label, y_label, error]);

  if (error || !tableData) {
    return tableData ? (
      <TableView columns={tableData.columns} rows={tableData.rows} caption={title} />
    ) : (
      <div className="rounded-card border border-hairline bg-surface-1 p-body text-text-faint italic">
        (Invalid chart data)
      </div>
    );
  }

  return (
    <div className="my-inline rounded-card border border-hairline bg-surface-1 p-body">
      <div className="h-[300px] w-full">
        <canvas ref={canvasRef} />
      </div>
      {title && (
        <div className="mt-hair text-center font-ui text-[0.74rem] text-text-faint">{title}</div>
      )}
    </div>
  );
}

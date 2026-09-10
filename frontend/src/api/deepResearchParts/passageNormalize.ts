/**
 * Passage-text normalization — mirrors report_export.py's shared seam
 * (_report_normalize.py). Scraped passages arrive with backslash-escaped
 * markdown links and table debris; the server export cleans both in
 * _normalize_passage_text. This is the SAME algorithm so the client-side
 * serializer stays byte-identical to the server's fmt=md output for the same
 * report. Private decomposition of api/deepResearch.ts (the api/agentParts
 * pattern) — the serializer seam stays @/api/deepResearch.
 */

const ESCAPED_LINK_RE = /\\\[((?:[^\\\]]|\\.)*?)\\\]\(([^()\s]+)\)/g;
const MD_ESCAPE_CHAR_RE = /\\([\\`*_{}[\]()<>#+\-.!|~])/g;
const EXTERNAL_URL_RE = /^https?:\/\//i;
const TABLE_ALIGN_RE = /^:?-{3,}:?$/;
const FENCE_RE = /^\s*(```|~~~)/;

function unescapeMd(text: string): string {
  return text.replace(MD_ESCAPE_CHAR_RE, "$1");
}

function rewriteEscapedLink(_m: string, rawText: string, rawUrl: string): string {
  const text = unescapeMd(rawText).trim();
  const url = unescapeMd(rawUrl).trim();
  if (!text) return EXTERNAL_URL_RE.test(url) ? url : "";
  if (EXTERNAL_URL_RE.test(url) && url !== text) return `${text} (${url})`;
  return text;
}

function splitTableRow(line: string): string[] {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|")) row = row.slice(0, -1);
  const cells: string[] = [];
  let buf = "";
  let escaped = false;
  for (const ch of row) {
    if (escaped) {
      buf += ch;
      escaped = false;
    } else if (ch === "\\") {
      escaped = true;
    } else if (ch === "|") {
      cells.push(buf.trim());
      buf = "";
    } else {
      buf += ch;
    }
  }
  if (escaped) buf += "\\";
  cells.push(buf.trim());
  return cells;
}

function isTableSeparator(line: string): boolean {
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every((c) => TABLE_ALIGN_RE.test(c.trim()));
}

function isPseudoTableLine(line: string): boolean {
  return (line.match(/\|/g) ?? []).length >= 3 && (line.match(/ \| /g) ?? []).length >= 2;
}

function formatPipeRow(cells: string[]): string {
  return `| ${cells.join(" | ")} |`;
}

function fitRow(cells: string[], width: number, pad: string): string[] {
  return cells.length < width
    ? cells.concat(Array(width - cells.length).fill(pad))
    : cells.slice(0, width);
}

/** Consume one genuine pipe table starting at `lines[i]` (whose header line,
 * already link-rewritten, is `header`): pad/truncate the separator row and
 * every body row to the header's column count. Appends the repaired rows to
 * `out` and returns the index of the first line past the table.
 * Mirrors _normalize_table_block in report_export's _report_normalize.py. */
function consumeTableBlock(lines: string[], i: number, header: string, out: string[]): number {
  const width = splitTableRow(header).length;
  out.push(header);
  const sep = splitTableRow(lines[i + 1]);
  out.push(sep.length === width ? lines[i + 1] : formatPipeRow(fitRow(sep, width, "---")));
  const isRow = (l: string) => Boolean(l.trim()) && l.includes("|") && !isTableSeparator(l);
  for (i += 2; i < lines.length && isRow(lines[i]); i += 1) {
    const rowLine = lines[i].replace(ESCAPED_LINK_RE, rewriteEscapedLink);
    const row = splitTableRow(rowLine);
    out.push(row.length === width ? rowLine : formatPipeRow(fitRow(row, width, "")));
  }
  return i;
}

/** Shared cleanup for passage-bearing markdown (summary + section bodies):
 * escaped links → clean prose, ragged pipe tables → header-width rows,
 * high-pipe-density non-table lines → readable `;`-separated text.
 * Fenced code blocks pass through untouched. Clean input is returned as-is. */
export function normalizePassageMarkdown(text: string): string {
  const lines = text.split("\n");
  const out: string[] = [];
  let i = 0;
  let inFence = false;
  while (i < lines.length) {
    let line = lines[i];
    if (FENCE_RE.test(line)) {
      inFence = !inFence;
      out.push(line);
      i += 1;
      continue;
    }
    if (inFence) {
      out.push(line);
      i += 1;
      continue;
    }
    line = line.replace(ESCAPED_LINK_RE, rewriteEscapedLink);
    if (i + 1 < lines.length && line.includes("|") && isTableSeparator(lines[i + 1])) {
      i = consumeTableBlock(lines, i, line, out);
      continue;
    }
    if (isPseudoTableLine(line)) {
      out.push(splitTableRow(line).filter(Boolean).join("; "));
      i += 1;
      continue;
    }
    out.push(line);
    i += 1;
  }
  let result = out.join("\n");
  if (text.endsWith("\n") && !result.endsWith("\n")) result += "\n";
  return result;
}

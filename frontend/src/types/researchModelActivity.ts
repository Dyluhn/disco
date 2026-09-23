export interface ModelActivityDetails {
  state?: "waiting" | "generating" | "backoff";
  source_id?: string;
  chunk?: number;
  chunks?: number;
  attempt?: number;
  retry_after_s?: number;
  http_status?: number;
}

export interface ModelActivityDetails {
  state?: "waiting" | "generating" | "backoff";
  reasoning_control_fallback?: boolean;
  source_id?: string;
  chunk?: number;
  chunks?: number;
  attempt?: number;
  retry_after_s?: number;
  http_status?: number;
}

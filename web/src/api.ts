import dayjs from "dayjs";

export interface DatasetSummary {
  dataset: string;
  domain: string;
  description: string;
  semantic_version: number;
  pit_class: string;
  business_key: string[];
  physical_key: string[];
  grain: string;
  canonical_table: string;
  read_model: string;
  partition_strategy: string;
  update_frequency: string;
  quality_rules: number;
  upstream: string[];
}

export interface FieldOut {
  name: string;
  type: string;
  unit: string | null;
  nullable: boolean;
  enum: string[] | null;
  precision: number | null;
  scale: number | null;
  description: string;
  pit_role: string;
}

export interface DatasetDetail extends DatasetSummary {
  update_sla: Record<string, string>;
  fields: FieldOut[];
  coverage: Record<string, unknown>;
  storage: Record<string, unknown>;
  quality: Record<string, unknown>[];
  lineage: { upstream: { dataset: string; fields?: string[] }[]; transform: string };
  derived: Record<string, unknown>[];
  sources: { provider: string; endpoint: string; note?: string | null }[];
  mappings: Record<string, unknown>[];
}

export interface EntitySummary {
  entity_id: number;
  entity_type: string;
  entity_class: string | null;
  market: string | null;
  code: string;
  name: string;
  currency: string | null;
  exchange: string | null;
  social_status: string | null;
  valid_from: string | null;
  valid_to: string | null;
  knowledge_time: string | null;
  version: number;
}

export interface EntityDetail extends EntitySummary {
  history: EntitySummary[];
  code_history: { code: string; valid_from: string | null; valid_to: string | null; version: number }[];
  relations: {
    relation_type: string;
    direction: "out" | "in";
    related_id: number;
    related_code: string | null;
    related_name: string | null;
    valid_from: string | null;
    valid_to: string | null;
  }[];
  external_ids: { id_type: string; id_value: string; valid_from: string | null; valid_to: string | null }[];
}

export interface JobRun {
  run_id: number;
  job_key: string;
  job_id: string;
  kind: string;
  dataset: string;
  scope: string;
  status: string;
  attempt: number;
  max_attempts: number;
  priority: number;
  scheduled_at: string;
  window_start: string | null;
  window_end: string | null;
  started_at: string | null;
  finished_at: string | null;
  rows_written: number | null;
  error: string | null;
  request_id: string | null;
  worker: string | null;
}

export interface Watermark {
  dataset: string;
  scope: string;
  watermark_time: string | null;
}

export interface SyncRequest {
  codes: string[];
  dataset?: string;
  start?: string | null;
  end?: string | null;
  request_id?: string | null;
  priority?: number;
}

export interface SyncItem {
  code: string;
  job_id: string;
  run_id: number | null;
  status: string;
  window_start: string | null;
  window_end: string | null;
  note: string | null;
}

export interface SyncResponse {
  submitted: SyncItem[];
  skipped: SyncItem[];
}

export interface Health {
  ok: boolean;
  checks: Record<string, boolean>;
  errors: string[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    detail: string,
  ) {
    super(detail);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as
      | { detail?: unknown }
      | null;
    const detail =
      typeof body?.detail === "string"
        ? body.detail
        : JSON.stringify(body?.detail ?? response.statusText);
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<Health>("/healthz"),
  datasets: () => request<DatasetSummary[]>("/v1/datasets"),
  dataset: (name: string) =>
    request<DatasetDetail>(`/v1/datasets/${encodeURIComponent(name)}`),
  entities: (params: {
    query?: string;
    entity_type?: string;
    market?: string;
    limit?: number;
    offset?: number;
  }) => request<{ total: number; limit: number; offset: number; items: EntitySummary[] }>(
    `/v1/entities?${new URLSearchParams(
      Object.entries(params)
        .filter(([, value]) => value !== undefined && value !== "")
        .map(([key, value]) => [key, String(value)]),
    )}`,
  ),
  entity: (id: number) => request<EntityDetail>(`/v1/entities/${id}`),
  jobs: (params: { status?: string; job_id?: string; limit?: number }) =>
    request<JobRun[]>(
      `/v1/jobs?${new URLSearchParams(
        Object.entries(params)
          .filter(([, value]) => value !== undefined && value !== "")
          .map(([key, value]) => [key, String(value)]),
      )}`,
    ),
  job: (runId: number) => request<JobRun>(`/v1/jobs/${runId}`),
  watermarks: () => request<Watermark[]>("/v1/watermarks"),
  sync: (payload: SyncRequest) =>
    request<SyncResponse>("/v1/jobs/sync", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};

export const JOB_STATUSES = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "retrying",
  "dead",
  "interrupted",
  "cancelled",
] as const;

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format("YYYY-MM-DD HH:mm:ss") : String(value);
}

export function formatDuration(start: string | null, end: string | null): string {
  if (!start || !end) return "—";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`;
}

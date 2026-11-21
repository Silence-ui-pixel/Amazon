import { request, type InputRun } from './images';
import type { Phrase } from './cleaning';

export interface TrafficReview {
  expected_version: number;
  keyword: string;
  decision: 'keep' | 'exclude' | 'defer';
  priority: 'high' | 'normal' | 'low';
  style_similar: boolean | null;
  price_delta_ratio: string | null;
  first_half_result_share: string | null;
  reason: string;
  evidence_note: string;
  reviewer: string;
}
export interface ReviewRecord {
  version: number;
  created_at: string;
  data: TrafficReview;
  relevance: 'high' | 'low' | 'unknown';
}
export interface TrafficReport {
  id: string;
  project_id: string;
  run_id: string;
  created_at: string;
  request: { cleaning_id: string; cleaning_version: number };
  rules: {
    id: string;
    purchase_threshold: string;
    monopoly_first: string;
    monopoly_three: string;
    price_delta_max: string;
    result_share_min: string;
    deep_rank_max: number;
  };
  total: number;
  required: number;
  reviewed: number;
  version: number;
  complete: boolean;
  stale: boolean;
}
export interface TrafficReviewProgress {
  analysis_id: string;
  project_id: string;
  run_id: string;
  status: 'idle' | 'queued' | 'running' | 'succeeded' | 'failed';
  attempt: number;
  total: number;
  required: number;
  reviewed: number;
  version: number;
  complete: boolean;
  stale: boolean;
  error_code: string;
  error: string;
  updated_at: string;
}
export interface TrafficReference {
  provider: 'sellersprite';
  direction: 'forward' | 'reverse';
  query: string;
  period_start: string | null;
  period_end: string | null;
  period_kind?: 'date_range' | 'last_30_days';
  collected_at?: string | null;
  weekly_rank: number | null;
  monthly_searches: number | null;
}
export interface SourceMetricRow {
  source_id: string;
  provider: 'aba' | 'sellersprite';
  direction: 'forward' | 'reverse';
  query: string;
  source_row: number;
  original: string;
  period_start: string | null;
  period_end: string | null;
  period_kind: 'date_range' | 'last_30_days';
  collected_at: string | null;
  metrics: TrafficRow['metrics'];
}
export interface TrafficRow {
  phrase: Phrase;
  metrics: {
    rank: number | null;
    purchase_ratio: string | null;
    purchase: boolean | null;
    monopoly: boolean | null;
    conversion_sum3: string | null;
    deep_analysis: boolean;
    missing: string[];
  };
  cleaning_exclusion: string;
  references: TrafficReference[];
  source_metrics: SourceMetricRow[];
  review: ReviewRecord | null;
}
export interface TrafficPage {
  total: number;
  offset: number;
  limit: number;
  items: TrafficRow[];
}
const object = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const count = (v: unknown): v is number =>
  typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
const decimal = (v: unknown): v is string =>
  typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v));
function validReview(v: unknown): boolean {
  if (!object(v) || !object(v.data)) return false;
  const d = v.data;
  return (
    count(v.version) &&
    typeof v.created_at === 'string' &&
    ['high', 'low', 'unknown'].includes(String(v.relevance)) &&
    count(d.expected_version) &&
    ['keyword', 'reason', 'evidence_note', 'reviewer'].every((k) => typeof d[k] === 'string') &&
    ['keep', 'exclude', 'defer'].includes(String(d.decision)) &&
    ['high', 'normal', 'low'].includes(String(d.priority)) &&
    (d.style_similar === null || typeof d.style_similar === 'boolean') &&
    [d.price_delta_ratio, d.first_half_result_share].every((x) => x === null || decimal(x))
  );
}
function validReport(v: unknown): v is TrafficReport {
  if (!object(v) || !object(v.request) || !object(v.rules)) return false;
  const r = v.rules;
  return (
    ['id', 'project_id', 'run_id', 'created_at'].every((k) => typeof v[k] === 'string') &&
    ['total', 'required', 'reviewed', 'version'].every((k) => count(v[k])) &&
    typeof v.stale === 'boolean' &&
    typeof v.complete === 'boolean' &&
    typeof v.request.cleaning_id === 'string' &&
    count(v.request.cleaning_version) &&
    typeof r.id === 'string' &&
    count(r.deep_rank_max) &&
    [
      'purchase_threshold',
      'monopoly_first',
      'monopoly_three',
      'price_delta_max',
      'result_share_min',
    ].every((k) => decimal(r[k]))
  );
}
function validRow(v: unknown): boolean {
  if (!object(v) || !object(v.phrase) || !object(v.metrics)) return false;
  const m = v.metrics;
  return (
    typeof v.phrase.keyword === 'string' &&
    Array.isArray(v.phrase.evidence) &&
    v.phrase.evidence.every(
      (e) =>
        object(e) &&
        ['source_id', 'query', 'original', 'payload'].every((k) => typeof e[k] === 'string') &&
        count(e.source_row),
    ) &&
    typeof v.cleaning_exclusion === 'string' &&
    (v.review === null || validReview(v.review)) &&
    Array.isArray(v.references) &&
    v.references.every(
      (r) =>
        object(r) &&
        r.provider === 'sellersprite' &&
        ['forward', 'reverse'].includes(String(r.direction)) &&
        typeof r.query === 'string' &&
        ['period_start', 'period_end'].every((k) => r[k] === null || typeof r[k] === 'string') &&
        (r.weekly_rank === null || count(r.weekly_rank)) &&
        (r.monthly_searches === null || count(r.monthly_searches)),
    ) &&
    Array.isArray(v.source_metrics) &&
    v.source_metrics.every(
      (s) =>
        object(s) &&
        object(s.metrics) &&
        typeof s.source_id === 'string' &&
        ['aba', 'sellersprite'].includes(String(s.provider)) &&
        ['forward', 'reverse'].includes(String(s.direction)) &&
        ['query', 'original'].every((k) => typeof s[k] === 'string') &&
        count(s.source_row) &&
        ['period_start', 'period_end', 'collected_at'].every(
          (k) => s[k] === null || typeof s[k] === 'string',
        ) &&
        ['date_range', 'last_30_days'].includes(String(s.period_kind)) &&
        (s.metrics.rank === null || count(s.metrics.rank)) &&
        [s.metrics.purchase_ratio, s.metrics.conversion_sum3].every(
          (x) => x === null || decimal(x),
        ) &&
        [s.metrics.purchase, s.metrics.monopoly].every(
          (x) => x === null || typeof x === 'boolean',
        ) &&
        typeof s.metrics.deep_analysis === 'boolean' &&
        Array.isArray(s.metrics.missing) &&
        s.metrics.missing.every((x) => typeof x === 'string'),
    ) &&
    (m.rank === null || count(m.rank)) &&
    [m.purchase_ratio, m.conversion_sum3].every((x) => x === null || decimal(x)) &&
    [m.purchase, m.monopoly].every((x) => x === null || typeof x === 'boolean') &&
    typeof m.deep_analysis === 'boolean' &&
    Array.isArray(m.missing) &&
    m.missing.every((x) => typeof x === 'string')
  );
}
function report(v: unknown): TrafficReport {
  if (!validReport(v)) throw new Error('流量筛选响应格式不正确');
  return v;
}
const base = (run: InputRun) =>
  `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/traffic`;
function progress(value: unknown): TrafficReviewProgress {
  if (
    !object(value) ||
    !['idle', 'queued', 'running', 'succeeded', 'failed'].includes(String(value.status)) ||
    !['analysis_id', 'project_id', 'run_id', 'error_code', 'error', 'updated_at'].every(
      (k) => typeof value[k] === 'string',
    ) ||
    !['attempt', 'total', 'required', 'reviewed', 'version'].every((k) => count(value[k])) ||
    typeof value.complete !== 'boolean' ||
    typeof value.stale !== 'boolean'
  )
    throw new Error('模型审核进度格式不正确');
  return value as unknown as TrafficReviewProgress;
}
export const trafficApi = {
  async progress(run: InputRun, id: string) {
    return progress(await request<unknown>(`${base(run)}/${encodeURIComponent(id)}/auto-review`));
  },
  async startReview(run: InputRun, id: string) {
    return progress(
      await request<unknown>(`${base(run)}/${encodeURIComponent(id)}/auto-review`, {
        method: 'POST',
      }),
    );
  },
  async batch(
    run: InputRun,
    id: string,
    data: {
      expected_version: number;
      keywords: string[];
      decision: TrafficReview['decision'];
      priority: TrafficReview['priority'];
      reason: string;
      evidence_note: string;
      reviewer: string;
    },
  ) {
    return report(
      await request<unknown>(`${base(run)}/${encodeURIComponent(id)}/batch-review`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      }),
    );
  },
  async history(run: InputRun) {
    const value = await request<unknown>(base(run));
    if (!Array.isArray(value) || !value.every(validReport)) throw new Error('筛选历史格式不正确');
    return value;
  },
  async create(run: InputRun, cleaningId: string, version: number) {
    return report(
      await request<unknown>(`${base(run)}?auto_review=true`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cleaning_id: cleaningId, cleaning_version: version }),
      }),
    );
  },
  async rows(run: InputRun, id: string, offset: number): Promise<TrafficPage> {
    const v = await request<unknown>(
      `${base(run)}/${encodeURIComponent(id)}/rows?offset=${offset}&limit=20`,
    );
    if (
      !object(v) ||
      !count(v.total) ||
      !count(v.offset) ||
      !count(v.limit) ||
      !Array.isArray(v.items) ||
      !v.items.every(validRow)
    )
      throw new Error('筛选分页格式不正确');
    return v as unknown as TrafficPage;
  },
  async review(run: InputRun, id: string, data: TrafficReview) {
    return report(
      await request<unknown>(`${base(run)}/${encodeURIComponent(id)}/review`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      }),
    );
  },
  async reviews(run: InputRun, id: string): Promise<ReviewRecord[]> {
    const v = await request<unknown>(`${base(run)}/${encodeURIComponent(id)}/reviews`);
    if (!Array.isArray(v) || !v.every(validReview)) throw new Error('审核历史格式不正确');
    return v as ReviewRecord[];
  },
  async download(run: InputRun, id: string) {
    const response = await fetch(`${base(run)}/${encodeURIComponent(id)}/export`);
    if (!response.ok) throw new Error('下载筛选结果失败');
    const url = URL.createObjectURL(await response.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = `traffic-${id}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
};

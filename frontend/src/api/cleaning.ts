import { request, type InputRun } from './images';

export type Category = 'selling_point' | 'scene' | 'preference' | 'unclassified';
export interface RootReview {
  term: string;
  category: Category;
  evidence: string;
}
export interface Exclusion {
  keyword: string;
  reason: string;
}
export interface Review {
  expected_version: number;
  roots: RootReview[];
  exclusions: Exclusion[];
  reason: string;
}
export interface CleaningRequest {
  aba_week_end: string;
  seller_period?: 'date_range' | 'last_30_days';
  seller_start: string | null;
  seller_end: string | null;
  text_only_acknowledged: boolean;
}
export interface Source {
  id: string;
  provider: 'aba' | 'sellersprite';
  direction: 'forward' | 'reverse';
  query: string;
  period_start: string | null;
  period_end: string | null;
  period_kind?: 'date_range' | 'last_30_days';
  collected_at?: string | null;
}
export interface CleaningReport {
  id: string;
  project_id: string;
  run_id: string;
  created_at: string;
  request: CleaningRequest;
  sources: Source[];
  algorithm_version: string;
  dictionary_version: string;
  phrase_count: number;
  token_count: number;
  frequency_count: number;
  version: number;
  review: Review | null;
  stale: boolean;
}
export interface Frequency {
  term: string;
  occurrences: number;
  phrase_count: number;
  density: number;
  suggestions: Category[];
}
export interface Evidence {
  source_id: string;
  query: string;
  original: string;
  source_row: number;
  payload: string;
}
export interface Phrase {
  keyword: string;
  evidence: Evidence[];
}
export interface ClassificationProgress {
  status: 'not_started' | 'running' | 'failed' | 'outcome_pending' | 'succeeded';
  completed: number;
  total: number;
  next_retry_at?: string | null;
}
export interface CleaningPage {
  total: number;
  offset: number;
  limit: number;
  phrases: Phrase[];
  frequencies: Frequency[];
}
const record = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const count = (v: unknown): v is number =>
  typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
const category = (v: unknown): v is Category =>
  v === 'selling_point' || v === 'scene' || v === 'preference' || v === 'unclassified';
function validReview(v: unknown): v is Review {
  return (
    record(v) &&
    count(v.expected_version) &&
    typeof v.reason === 'string' &&
    Array.isArray(v.roots) &&
    v.roots.every(
      (r) =>
        record(r) &&
        typeof r.term === 'string' &&
        category(r.category) &&
        typeof r.evidence === 'string',
    ) &&
    Array.isArray(v.exclusions) &&
    v.exclusions.every(
      (e) => record(e) && typeof e.keyword === 'string' && typeof e.reason === 'string',
    )
  );
}
function validReport(v: unknown): v is CleaningReport {
  if (!record(v) || !record(v.request)) return false;
  const q = v.request;
  return (
    ['id', 'project_id', 'run_id', 'created_at', 'algorithm_version', 'dictionary_version'].every(
      (k) => typeof v[k] === 'string',
    ) &&
    ['phrase_count', 'token_count', 'frequency_count', 'version'].every((k) => count(v[k])) &&
    typeof v.stale === 'boolean' &&
    typeof q.aba_week_end === 'string' &&
    ['seller_start', 'seller_end'].every((k) => q[k] === null || typeof q[k] === 'string') &&
    q.text_only_acknowledged === true &&
    (v.review === null || validReview(v.review)) &&
    Array.isArray(v.sources) &&
    v.sources.every(
      (s) =>
        record(s) &&
        ['id', 'query'].every((k) => typeof s[k] === 'string') &&
        ['period_start', 'period_end'].every((k) => s[k] === null || typeof s[k] === 'string') &&
        ['aba', 'sellersprite'].includes(String(s.provider)) &&
        ['forward', 'reverse'].includes(String(s.direction)),
    )
  );
}
function report(v: unknown): CleaningReport {
  if (!validReport(v)) throw new Error('词频分析响应格式不正确');
  return v;
}
function page(v: unknown): CleaningPage {
  if (
    !record(v) ||
    !count(v.total) ||
    !count(v.offset) ||
    !count(v.limit) ||
    !Array.isArray(v.frequencies) ||
    !v.frequencies.every(
      (f) =>
        record(f) &&
        typeof f.term === 'string' &&
        count(f.occurrences) &&
        count(f.phrase_count) &&
        typeof f.density === 'number' &&
        Number.isFinite(f.density) &&
        Array.isArray(f.suggestions) &&
        f.suggestions.every(category),
    ) ||
    !Array.isArray(v.phrases) ||
    !v.phrases.every(
      (p) =>
        record(p) &&
        typeof p.keyword === 'string' &&
        Array.isArray(p.evidence) &&
        p.evidence.every(
          (e) =>
            record(e) &&
            ['source_id', 'query', 'original', 'payload'].every((k) => typeof e[k] === 'string') &&
            count(e.source_row),
        ),
    )
  )
    throw new Error('词频分页响应格式不正确');
  return v as unknown as CleaningPage;
}
const base = (run: InputRun) =>
  `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/cleaning`;
export const cleaningApi = {
  async history(run: InputRun) {
    const v = await request<unknown>(base(run));
    if (!Array.isArray(v) || !v.every(validReport)) throw new Error('分析历史响应格式不正确');
    return v;
  },
  async create(run: InputRun, data: CleaningRequest) {
    return report(
      await request<unknown>(base(run), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      }),
    );
  },
  async review(run: InputRun, id: string, data: Review) {
    return report(
      await request<unknown>(`${base(run)}/${encodeURIComponent(id)}/review`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      }),
    );
  },
  async progress(run: InputRun, id: string): Promise<ClassificationProgress> {
    const v = await request<unknown>(`${base(run)}/${encodeURIComponent(id)}/classification-progress`);
    if (!record(v) || !['not_started', 'running', 'failed', 'outcome_pending', 'succeeded'].includes(String(v.status)) || !count(v.completed) || !count(v.total) || v.completed > v.total) {
      throw new Error('分类进度响应格式不正确');
    }
    return v as unknown as ClassificationProgress;
  },
  async classify(run: InputRun, id: string) {
    return report(
      await request<unknown>(`${base(run)}/${encodeURIComponent(id)}/classify`, {
        method: 'POST',
      }),
    );
  },
  async retryClassification(run: InputRun, id: string) {
    return report(
      await request<unknown>(`${base(run)}/${encodeURIComponent(id)}/classify?retry_failed=true`, {
        method: 'POST',
      }),
    );
  },
  async rows(run: InputRun, id: string, kind: 'phrases' | 'frequencies', offset: number) {
    return page(
      await request<unknown>(
        `${base(run)}/${encodeURIComponent(id)}/rows?kind=${kind}&offset=${offset}&limit=20`,
      ),
    );
  },
  async download(run: InputRun, id: string) {
    const response = await fetch(`${base(run)}/${encodeURIComponent(id)}/export`);
    if (!response.ok) throw new Error('下载分析失败');
    const url = URL.createObjectURL(await response.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = `cleaning-${id}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
};

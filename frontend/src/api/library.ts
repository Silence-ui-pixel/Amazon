import { request, type InputRun } from './images';
import type { TrafficRow } from './traffic';

export interface LibraryEntry {
  keyword: string;
  issues: string[];
  tier: 'L1' | 'L2' | 'L3' | 'L4' | 'unranked';
  roots: Record<string, string[]>;
  priority: 'high' | 'normal' | 'low';
  usage: 'pending' | 'listing' | 'advertising' | 'both' | 'paused';
  auxiliary_asins: string[];
  evidence: TrafficRow;
}
export interface EntryEdit {
  expected_version: number;
  keyword: string;
  priority: LibraryEntry['priority'];
  usage: LibraryEntry['usage'];
  auxiliary_asins: string[];
  reason: string;
  operator: string;
}
export interface LibraryRules {
  id: string;
  l1_max: number;
  l2_max: number;
  l3_max: number;
  asin_positions: number;
  suggestions_per_tier: number;
}
export interface LibraryReport {
  id: string;
  project_id: string;
  run_id: string;
  created_at: string;
  request: { traffic_id: string; traffic_version: number };
  version: number;
  publication_version: number | null;
  published_at: string | null;
  publication: {
    expected_version: number;
    publisher: string;
    reason: string;
    confirmed: true;
  } | null;
  stale: boolean;
  rules: LibraryRules;
  source_sha256: string;
  snapshots: Record<string, string>;
  entries: LibraryEntry[];
  suggestions: { tier: string; asin: string; keyword_count: number }[];
  edits: { created_at: string; version: number; data: EntryEdit }[];
}
const object = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const count = (v: unknown): v is number =>
  typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
const strings = (v: unknown): v is string[] =>
  Array.isArray(v) && v.every((x) => typeof x === 'string');
const libraryRules = (v: unknown): v is LibraryRules =>
  object(v) &&
  typeof v.id === 'string' &&
  ['l1_max', 'l2_max', 'l3_max', 'asin_positions', 'suggestions_per_tier'].every((k) =>
    count(v[k]),
  );
function valid(v: unknown): v is LibraryReport {
  if (!object(v) || !object(v.request) || !object(v.snapshots)) return false;
  return (
    ['id', 'project_id', 'run_id', 'created_at', 'source_sha256'].every(
      (k) => typeof v[k] === 'string',
    ) &&
    libraryRules(v.rules) &&
    count(v.version) &&
    (v.publication_version === null || count(v.publication_version)) &&
    typeof v.stale === 'boolean' &&
    typeof v.request.traffic_id === 'string' &&
    count(v.request.traffic_version) &&
    Object.values(v.snapshots).every((x) => typeof x === 'string') &&
    Array.isArray(v.entries) &&
    v.entries.every(
      (e) =>
        object(e) &&
        typeof e.keyword === 'string' &&
        strings(e.issues) &&
        ['L1', 'L2', 'L3', 'L4', 'unranked'].includes(String(e.tier)) &&
        object(e.roots) &&
        ['selling_point', 'scene', 'preference'].every((k) =>
          strings((e.roots as Record<string, unknown>)[k]),
        ) &&
        ['high', 'normal', 'low'].includes(String(e.priority)) &&
        ['pending', 'listing', 'advertising', 'both', 'paused'].includes(String(e.usage)) &&
        strings(e.auxiliary_asins) &&
        object(e.evidence) &&
        object(e.evidence.metrics) &&
        (e.evidence.metrics.rank === null || count(e.evidence.metrics.rank)) &&
        object(e.evidence.review) &&
        typeof e.evidence.review.relevance === 'string' &&
        object(e.evidence.review.data) &&
        typeof e.evidence.review.data.reason === 'string' &&
        typeof e.evidence.review.data.evidence_note === 'string',
    ) &&
    Array.isArray(v.suggestions) &&
    v.suggestions.every(
      (s) =>
        object(s) &&
        typeof s.tier === 'string' &&
        typeof s.asin === 'string' &&
        count(s.keyword_count),
    ) &&
    Array.isArray(v.edits) &&
    v.edits.every(
      (e) =>
        object(e) &&
        typeof e.created_at === 'string' &&
        count(e.version) &&
        object(e.data) &&
        typeof e.data.operator === 'string' &&
        typeof e.data.reason === 'string',
    )
  );
}
const base = (run: InputRun) =>
  `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/library`;
function parse(v: unknown): LibraryReport {
  if (!valid(v)) throw new Error('关键词库响应格式不正确');
  return v;
}
async function write(run: InputRun, path: string, method: string, data: unknown) {
  return parse(
    await request<unknown>(base(run) + path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),
  );
}
export interface LibraryTemplate {
  rule_notes?: string[];
  summary: string[][];
  headers: string[];
  rows: (string | number)[][];
}
export interface LibraryRelevanceJob {
  id: string;
  library_id: string;
  library_version: number;
  ranked_only: boolean;
  status: 'queued' | 'running' | 'completed' | 'failed';
  total: number;
  completed: number;
  failed: number;
  missing_samples: number;
  phase: '' | 'collecting' | 'verifying' | 'waiting' | 'exporting';
  next_retry_at?: string | null;
  current_keyword: string;
  error: string;
}
export const libraryApi = {
  relevanceLatest: (run: InputRun, id: string, rankedOnly: boolean) =>
    request<{ job: LibraryRelevanceJob | null }>(`${base(run)}/${encodeURIComponent(id)}/relevance-exports/latest?ranked_only=${rankedOnly}`)
      .then((value) => value.job),
  relevanceStart: (run: InputRun, id: string, rankedOnly: boolean) =>
    request<LibraryRelevanceJob>(`${base(run)}/${encodeURIComponent(id)}/relevance-exports`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ranked_only: rankedOnly }),
    }),
  relevanceGet: (run: InputRun, id: string, jobId: string) =>
    request<LibraryRelevanceJob>(`${base(run)}/${encodeURIComponent(id)}/relevance-exports/${encodeURIComponent(jobId)}`),
  relevanceRetry: (run: InputRun, id: string, jobId: string) =>
    request<LibraryRelevanceJob>(`${base(run)}/${encodeURIComponent(id)}/relevance-exports/${encodeURIComponent(jobId)}/retry`, { method: 'POST' }),
  relevanceFile: (run: InputRun, id: string, jobId: string) =>
    `${base(run)}/${encodeURIComponent(id)}/relevance-exports/${encodeURIComponent(jobId)}/file`,
  template: (run: InputRun, id: string) =>
    request<LibraryTemplate>(`${base(run)}/${encodeURIComponent(id)}/template`),
  // CSV 与 Excel 默认都只导出有有效官方 ABA 周排名的词；不删除库内未排名项。
  // 此处只传 ranked_only，页面搜索/层级筛选不是文件的导出条件。
  xlsxUrl: (run: InputRun, id: string, rankedOnly = true) =>
    `${base(run)}/${encodeURIComponent(id)}/template.xlsx?ranked_only=${rankedOnly}`,
  revise: (run: InputRun, report: LibraryReport) =>
    write(run, '', 'POST', { ...report.request, revision_of: report.id }),
  async history(run: InputRun) {
    const v = await request<unknown>(base(run));
    if (!Array.isArray(v) || !v.every(valid)) throw new Error('关键词库历史格式不正确');
    return v;
  },
  create: (run: InputRun, trafficId: string, version: number) =>
    write(run, '', 'POST', { traffic_id: trafficId, traffic_version: version }),
  edit: (run: InputRun, id: string, data: EntryEdit) =>
    write(run, `/${encodeURIComponent(id)}/entry`, 'PUT', data),
  refreshAttachments: (run: InputRun, report: LibraryReport) =>
    write(run, `/${encodeURIComponent(report.id)}/attachments/refresh`, 'POST', { expected_version: report.version }),
  publish: (run: InputRun, id: string, data: NonNullable<LibraryReport['publication']>) =>
    write(run, `/${encodeURIComponent(id)}/publish`, 'POST', data),
  async download(run: InputRun, id: string, rankedOnly = true) {
    const response = await fetch(
      `${base(run)}/${encodeURIComponent(id)}/export?ranked_only=${rankedOnly}`,
    );
    if (!response.ok) throw new Error('关键词库导出失败');
    const url = URL.createObjectURL(await response.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = `keyword-library-${id}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  },
};

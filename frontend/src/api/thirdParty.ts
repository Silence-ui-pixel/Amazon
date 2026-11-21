/** 第五步：导出词表导入。所有外部响应先校验再进入组件。 */
import { request, type InputRun } from './images';
import { normalizeBrowserJobs, type CollectionMethod, type VariantScope } from './thirdPartyJobs';

export type { BrowserJob } from './thirdPartyJobs';

export const importFields = {
  keyword: '关键词（必选）',
  translation: '关键词翻译',
  monthly_searches: '月搜索量',
  monthly_rank: 'ABA 月排名（第三方提供）',
  weekly_rank: 'ABA 周排名（第三方提供）',
  bid: 'PPC 竞价（数值）',
  top1_click_share: '第 1 商品点击份额',
  top1_conversion_share: '第 1 商品转化份额',
  top2_conversion_share: '第 2 商品转化份额',
  top3_conversion_share: '第 3 商品转化份额',
} as const;
export interface ImportMetadata {
  direction: 'forward' | 'reverse';
  query: string;
  collection_method: 'keyword_mining' | 'keyword_reverse' | 'traffic_extension';
  source_queries: string[];
  variant_scope: 'all' | 'best_selling' | 'current' | null;
  marketplace: string;
  period_start: string | null;
  period_end: string | null;
  period_kind?: 'date_range' | 'last_30_days';
  collected_at?: string | null;
  source_total: number | null;
  export_complete: boolean;
  batch_key: string;
  currency: string | null;
}
export interface ImportReport {
  id: string;
  metadata: ImportMetadata;
  filename: string;
  file_sha256: string;
  created_at: string;
  status: 'preview' | 'confirmed';
  stale: boolean;
  complete: boolean;
  imported_rows: number;
  unique_rows: number;
  headers: string[];
  samples: Array<Record<string, string>>;
  suggested_mapping: Record<string, string>;
  confirmation: { mapping: Record<string, string>; share_unit: 'fraction' | 'percent' } | null;
}
export interface ImportWorkspace {
  reports: ImportReport[];
  forward_queries: string[];
  reverse_queries: string[];
  covered_forward: number;
  covered_reverse: number;
  complete: boolean;
}
export interface ImportRow {
  row_number: number;
  keyword: string;
  normalized_keyword: string;
  translation: string | null;
  monthly_searches: number | null;
  monthly_rank: number | null;
  weekly_rank: number | null;
  bid: string | null;
  top1_click_share: string | null;
  top1_conversion_share: string | null;
  top2_conversion_share: string | null;
  top3_conversion_share: string | null;
  raw: Record<string, string>;
}
export interface ImportPage {
  total: number;
  items: ImportRow[];
}
const record = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const strings = (v: unknown): v is string[] =>
  Array.isArray(v) && v.every((x) => typeof x === 'string');
const stringMap = (v: unknown): v is Record<string, string> =>
  record(v) && Object.values(v).every((x) => typeof x === 'string');
const count = (v: unknown) => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
function validReport(v: unknown): v is ImportReport {
  if (!record(v) || !record(v.metadata)) return false;
  const m = v.metadata;
  return (
    ['id', 'filename', 'file_sha256', 'created_at'].every((k) => typeof v[k] === 'string') &&
    ['preview', 'confirmed'].includes(String(v.status)) &&
    typeof v.stale === 'boolean' &&
    typeof v.complete === 'boolean' &&
    count(v.imported_rows) &&
    count(v.unique_rows) &&
    strings(v.headers) &&
    Array.isArray(v.samples) &&
    v.samples.every(stringMap) &&
    stringMap(v.suggested_mapping) &&
    (v.confirmation === null ||
      (record(v.confirmation) &&
        stringMap(v.confirmation.mapping) &&
        ['fraction', 'percent'].includes(String(v.confirmation.share_unit)))) &&
    ['forward', 'reverse'].includes(String(m.direction)) &&
    ['keyword_mining', 'keyword_reverse', 'traffic_extension'].includes(
      String(m.collection_method),
    ) &&
    strings(m.source_queries) &&
    (m.variant_scope === null ||
      ['all', 'best_selling', 'current'].includes(String(m.variant_scope))) &&
    ['query', 'marketplace', 'batch_key'].every((k) => typeof m[k] === 'string') &&
    ['period_start', 'period_end'].every((k) => m[k] === null || typeof m[k] === 'string') &&
    typeof m.export_complete === 'boolean' &&
    (m.currency === null || (typeof m.currency === 'string' && /^[A-Z]{3}$/.test(m.currency))) &&
    (m.source_total === null || count(m.source_total))
  );
}
function report(v: unknown): ImportReport {
  if (!validReport(v)) throw new Error('第三方词表响应格式不正确，请重新读取');
  return v;
}
function workspace(v: unknown): ImportWorkspace {
  if (
    !record(v) ||
    !Array.isArray(v.reports) ||
    !v.reports.every(validReport) ||
    !strings(v.forward_queries) ||
    !strings(v.reverse_queries) ||
    !count(v.covered_forward) ||
    !count(v.covered_reverse) ||
    typeof v.complete !== 'boolean'
  )
    throw new Error('第三方词表状态格式不正确，请重新读取');
  return v as unknown as ImportWorkspace;
}
function page(v: unknown): ImportPage {
  if (
    !record(v) ||
    !count(v.total) ||
    !Array.isArray(v.items) ||
    !v.items.every(
      (x) =>
        record(x) &&
        count(x.row_number) &&
        typeof x.keyword === 'string' &&
        typeof x.normalized_keyword === 'string' &&
        (x.translation === null || typeof x.translation === 'string') &&
        ['monthly_searches', 'monthly_rank', 'weekly_rank'].every(
          (k) => x[k] === null || count(x[k]),
        ) &&
        [
          'bid',
          'top1_click_share',
          'top1_conversion_share',
          'top2_conversion_share',
          'top3_conversion_share',
        ].every(
          (k) =>
            x[k] === null ||
            (typeof x[k] === 'string' &&
              /^\d+(\.\d+)?([eE][+-]?\d+)?$/.test(x[k]) &&
              Number.isFinite(Number(x[k]))),
        ) &&
        stringMap(x.raw),
    )
  )
    throw new Error('第三方词表行数据格式不正确');
  return v as unknown as ImportPage;
}
const base = (run: InputRun) =>
  `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/third-party`;
export const thirdPartyApi = {
  async jobs(run: InputRun) {
    return normalizeBrowserJobs(await request<unknown>(base(run) + '/browser-jobs'));
  },
  async collect(
    run: InputRun,
    data: {
      direction: 'forward' | 'reverse';
      query: string;
      method: CollectionMethod;
      queries: string[];
      variant_scope: VariantScope | null;
      period: 'last_30_days';
    },
  ) {
    const value = await request<unknown>(base(run) + '/browser-jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    return normalizeBrowserJobs([value])[0]!;
  },
  originalUrl: (run: InputRun, id: string) =>
    base(run) + '/' + encodeURIComponent(id) + '/original',

  async get(run: InputRun) {
    return workspace(await request<unknown>(base(run) + '?period_kind=last_30_days'));
  },
  async preview(run: InputRun, file: File, metadata: ImportMetadata) {
    const body = new FormData();
    body.append('file', file);
    body.append('metadata', JSON.stringify(metadata));
    return report(await request<unknown>(`${base(run)}/preview`, { method: 'POST', body }));
  },
  async confirm(
    run: InputRun,
    id: string,
    mapping: Record<string, string>,
    share_unit: 'fraction' | 'percent',
  ) {
    return report(
      await request<unknown>(`${base(run)}/${encodeURIComponent(id)}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mapping, share_unit }),
      }),
    );
  },
  async rows(run: InputRun, id: string, offset: number) {
    return page(
      await request<unknown>(
        `${base(run)}/${encodeURIComponent(id)}/rows?offset=${offset}&limit=20`,
      ),
    );
  },
  async download(run: InputRun, id: string) {
    const result = await fetch(`${base(run)}/${encodeURIComponent(id)}/export`);
    if (!result.ok) throw new Error(`词表下载失败（${result.status}）`);
    const url = URL.createObjectURL(await result.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = `sellersprite-${id}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
};

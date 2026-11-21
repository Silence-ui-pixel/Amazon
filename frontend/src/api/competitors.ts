/** 第三步边界验证；刷新只读取已保存快照。 */
import { ApiError, request, type InputRun } from './images';

export interface Review {
  asin: string;
  decision: 'pending' | 'include' | 'exclude';
  reason: string;
  price_override_reason: string;
}
export interface Competitor extends Review {
  title: string;
  image_url: string;
  price: string | null;
  currency: string | null;
  price_basis: string;
  price_status: string;
  keyword_hits: number;
  relevance_score: string | null;
  sources: Array<{
    kind: string;
    label: string;
    keyword: string;
    position: number | null;
    url: string;
    region: string;
    captured_at: string;
  }>;
}
export interface PoeDownload {
  id: string;
  filename: string;
  keyword: string;
  sha256: string;
  source_url: string;
  rows: number;
  intent_ref: string;
  intent_name: string;
  intent_reason: string;
  intent_confidence: '' | 'low' | 'medium' | 'high';
  status: 'completed' | 'failed';
  stage: '' | 'completed' | 'search' | 'archive' | 'selection' | 'parse' | 'intent_collection' | 'intent_selection' | 'download' | 'csv_validation';
  error: string;
  price_qualified_rows: number;
  // 当前后端未返回逐报表相关度计数；缺失表示未知，不能当作零。
  relevance_qualified_rows?: number;
}
export interface Competitors {
  poe_downloads?: PoeDownload[];
  id: string;
  core_selection_id: string;
  core_keywords: string[];
  version: number;
  stale: boolean;
  confirmed_at: string | null;
  reference_price: string | null;
  currency: string;
  tolerance: string;
  items: Competitor[];
  warnings: string[];
}
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
const strings = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((x) => typeof x === 'string');
const nullableString = (value: unknown) => value === null || typeof value === 'string';
function validSource(value: unknown): boolean {
  return (
    record(value) &&
    ['kind', 'label', 'keyword', 'url', 'region', 'captured_at'].every(
      (k) => typeof value[k] === 'string',
    ) &&
    (value.position === null ||
      (typeof value.position === 'number' && Number.isInteger(value.position)))
  );
}
function validItem(value: unknown): boolean {
  return (
    record(value) &&
    typeof value.asin === 'string' &&
    /^[A-Z0-9]{10}$/.test(value.asin) &&
    ['title', 'image_url', 'reason', 'price_override_reason'].every(
      (k) => typeof value[k] === 'string',
    ) &&
    ['pending', 'include', 'exclude'].includes(String(value.decision)) &&
    ['unknown', 'within', 'outside'].includes(String(value.price_status)) &&
    ['unknown', 'live', 'average_30d', 'average_90d', 'average_360d', 'export'].includes(
      String(value.price_basis),
    ) &&
    nullableString(value.price) &&
    nullableString(value.currency) &&
    nullableString(value.relevance_score) &&
    typeof value.keyword_hits === 'number' &&
    Array.isArray(value.sources) &&
    value.sources.every(validSource)
  );
}
function validate(value: unknown): Competitors {
  if (
    !record(value) ||
    typeof value.id !== 'string' ||
    typeof value.core_selection_id !== 'string' ||
    !strings(value.core_keywords) ||
    !strings(value.warnings) ||
    typeof value.version !== 'number' ||
    !Number.isInteger(value.version) ||
    typeof value.stale !== 'boolean' ||
    !nullableString(value.confirmed_at) ||
    !nullableString(value.reference_price) ||
    typeof value.currency !== 'string' ||
    typeof value.tolerance !== 'string' ||
    !Array.isArray(value.items) ||
    !value.items.every(validItem) ||
    (value.poe_downloads !== undefined &&
      (!Array.isArray(value.poe_downloads) ||
        !value.poe_downloads.every(
          (x: unknown) =>
            record(x) &&
            [
              'id',
              'filename',
              'keyword',
              'sha256',
              'source_url',
              'intent_ref',
              'intent_name',
              'intent_reason',
              'stage',
              'error',
            ].every(
              (k) => typeof x[k] === 'string',
            ) &&
            ['', 'low', 'medium', 'high'].includes(String(x.intent_confidence)) &&
            ['', 'completed', 'search', 'archive', 'selection', 'parse', 'intent_collection', 'intent_selection', 'download', 'csv_validation'].includes(
              String(x.stage),
            ) &&
            ['completed', 'failed'].includes(String(x.status)) &&
            ['rows', 'price_qualified_rows'].every(
              (k) => typeof x[k] === 'number' && Number.isInteger(x[k]),
            ) &&
            (x.relevance_qualified_rows === undefined ||
              (typeof x.relevance_qualified_rows === 'number' &&
                Number.isInteger(x.relevance_qualified_rows) &&
                x.relevance_qualified_rows >= 0)),
        )))
  )
    throw new Error('竞对接口返回结构不正确，请重新读取');
  // 所有消费字段已逐项验证；断言仅用于连接运行时校验与 TypeScript。
  return value as unknown as Competitors;
}
const base = (run: InputRun) =>
  `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/competitors`;
async function send(run: InputRun, path: string, method: string, data: unknown) {
  return validate(
    await request<unknown>(base(run) + path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),
  );
}
export const competitorApi = {
  async get(run: InputRun): Promise<Competitors | null> {
    try {
      return validate(await request<unknown>(base(run)));
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null;
      throw e;
    }
  },
  discover: (run: InputRun, data: unknown) => send(run, '/discover', 'POST', data),
  poeDownload: (run: InputRun, data: unknown) => send(run, '/poe-download', 'POST', data),
  poeDownloadAll: (run: InputRun, data: unknown) => send(run, '/poe-download-all', 'POST', data),
  originalUrl: (run: InputRun, id: string) =>
    base(run) + '/poe-downloads/' + encodeURIComponent(id),
  save: (run: InputRun, data: unknown) => send(run, '/selection', 'PUT', data),
};

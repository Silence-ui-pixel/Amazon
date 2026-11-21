/** 第四步：显式启动采集，恢复与轮询仅使用 GET。 */
import { request, type InputRun } from './images';

export type Direction = 'forward' | 'reverse';
export interface ExpansionReport {
  id: string;
  direction: Direction;
  week_start: string;
  week_end: string;
  created_at: string;
  status: 'running' | 'succeeded' | 'partial' | 'failed' | 'interrupted';
  stale: boolean;
  max_rows: number;
  warnings: string[];
  queries: Array<{
    query: string;
    status: 'legacy' | 'pending' | 'running' | 'succeeded' | 'failed' | 'interrupted';
    total: number;
    fetched: number;
    pages: number;
    truncated: boolean;
    stop_reason: string;
    error: string | null;
  }>;
}
const PARTIAL_FINISH_WARNING =
  '部分查询失败或达到采集边界，当前结果不代表穷尽；请查看各查询原因后显式重试。';

export function queryDownloadable(
  report: Pick<ExpansionReport, 'status' | 'warnings'>,
  query: ExpansionReport['queries'][number],
): boolean {
  if (query.fetched <= 0 || Boolean(query.error)) return false;
  if (query.status === 'succeeded') return true;
  if (query.status !== 'legacy') return false;
  return (
    report.status === 'succeeded' ||
    (report.status === 'partial' && report.warnings.includes(PARTIAL_FINISH_WARNING))
  );
}
export interface ExpansionWorkspace {
  reports: ExpansionReport[];
  complete: boolean;
  comparison: { both: number; forward_only: number; reverse_only: number };
}
export interface ExpansionPage {
  total: number;
  items: Array<{
    keyword: string;
    search_frequency_rank: number | null;
    rank_conflict: boolean;
    also_in_other_direction: boolean;
    evidence: Array<{
      query: string;
      keyword: string;
      search_frequency_rank: number;
      products: Array<{
        asin: string;
        click_share: number | null;
        conversion_share: number | null;
      }>;
    }>;
  }>;
}
function record(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}
const number = (v: unknown) => typeof v === 'number' && Number.isFinite(v);
const nullableNumber = (v: unknown) => v === null || number(v);
function validReport(v: unknown): boolean {
  return (
    record(v) &&
    ['id', 'week_start', 'week_end', 'created_at'].every((k) => typeof v[k] === 'string') &&
    ['forward', 'reverse'].includes(String(v.direction)) &&
    ['running', 'succeeded', 'partial', 'failed', 'interrupted'].includes(String(v.status)) &&
    typeof v.stale === 'boolean' &&
    number(v.max_rows) &&
    Array.isArray(v.warnings) &&
    v.warnings.every((x) => typeof x === 'string') &&
    Array.isArray(v.queries) &&
    v.queries.every(
      (q) =>
        record(q) &&
        typeof q.query === 'string' &&
        ['legacy', 'pending', 'running', 'succeeded', 'failed', 'interrupted'].includes(
          String(q.status),
        ) &&
        ['total', 'fetched', 'pages'].every((k) => number(q[k])) &&
        typeof q.truncated === 'boolean' &&
        typeof q.stop_reason === 'string' &&
        (q.error === null || typeof q.error === 'string'),
    )
  );
}
function workspace(v: unknown): ExpansionWorkspace {
  if (
    !record(v) ||
    !Array.isArray(v.reports) ||
    !v.reports.every(validReport) ||
    typeof v.complete !== 'boolean' ||
    !record(v.comparison) ||
    !['both', 'forward_only', 'reverse_only'].every((k) =>
      number((v.comparison as Record<string, unknown>)[k]),
    )
  )
    throw new Error('ABA 拓词接口返回结构不正确，请重新读取');
  return v as unknown as ExpansionWorkspace;
}
function page(v: unknown): ExpansionPage {
  if (
    !record(v) ||
    !number(v.total) ||
    !Array.isArray(v.items) ||
    !v.items.every(
      (x) =>
        record(x) &&
        typeof x.keyword === 'string' &&
        nullableNumber(x.search_frequency_rank) &&
        typeof x.rank_conflict === 'boolean' &&
        typeof x.also_in_other_direction === 'boolean' &&
        Array.isArray(x.evidence) &&
        x.evidence.every(
          (e) =>
            record(e) &&
            typeof e.query === 'string' &&
            typeof e.keyword === 'string' &&
            number(e.search_frequency_rank) &&
            Array.isArray(e.products) &&
            e.products.every(
              (p) =>
                record(p) &&
                typeof p.asin === 'string' &&
                nullableNumber(p.click_share) &&
                nullableNumber(p.conversion_share),
            ),
        ),
    )
  )
    throw new Error('ABA 词表返回结构不正确，请重新读取');
  return v as unknown as ExpansionPage;
}
const base = (run: InputRun) =>
  `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/aba-expansion`;
function downloadFilename(response: Response, fallback: string): string {
  const disposition = response.headers.get('Content-Disposition') ?? '';
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (!encoded) return fallback;
  try {
    return decodeURIComponent(encoded);
  } catch {
    return fallback;
  }
}
function safeQueryFilename(direction: Direction, query: string): string {
  if (direction === 'reverse') {
    const count = query.split(',').filter((asin) => asin.trim()).length;
    return `第四步ABA反向${count}个ASIN.xlsx`;
  }
  const invalidFilenameChars = new Set('<>:"/\\|?*');
  const cleaned =
    Array.from(query, (char) =>
      char.charCodeAt(0) < 32 || invalidFilenameChars.has(char) ? '-' : char,
    ).join('') || '核心大词';
  const prefix = '第四步ABA正向';
  const suffix = '.xlsx';
  const filename = `${prefix}${cleaned}${suffix}`;
  if (filename.length <= 255) return filename;
  let digest = 0xcbf29ce484222325n;
  for (const byte of new TextEncoder().encode(query)) {
    digest ^= BigInt(byte);
    digest = BigInt.asUintN(64, digest * 0x100000001b3n);
  }
  const marker = `~${digest.toString(16).padStart(16, '0')}`;
  const available = 255 - prefix.length - marker.length - suffix.length;
  let end = available;
  const previous = cleaned.charCodeAt(end - 1);
  const next = cleaned.charCodeAt(end);
  if (previous >= 0xd800 && previous <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) end--;
  return `${prefix}${cleaned.slice(0, end)}${marker}${suffix}`;
}
async function saveResponse(response: Response, filename: string): Promise<void> {
  const url = URL.createObjectURL(await response.blob());
  const a = document.createElement('a');
  a.href = url;
  a.download = downloadFilename(response, filename);
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export const expansionApi = {
  async get(run: InputRun, week: string) {
    return workspace(await request<unknown>(`${base(run)}?week_end=${encodeURIComponent(week)}`));
  },
  async collect(run: InputRun, direction: Direction, week_end: string, max_rows: number) {
    const result = await request<unknown>(base(run), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ direction, week_end, max_rows }),
    });
    if (!validReport(result)) throw new Error('ABA 采集接口返回结构不正确');
    return result as ExpansionReport;
  },
  async rows(run: InputRun, id: string, offset: number) {
    return page(
      await request<unknown>(
        `${base(run)}/${encodeURIComponent(id)}/rows?offset=${offset}&limit=20`,
      ),
    );
  },
  async download(run: InputRun, report: ExpansionReport) {
    const response = await fetch(`${base(run)}/${encodeURIComponent(report.id)}/export`);
    if (!response.ok) throw new Error(`词表下载失败（${response.status}），请重新读取状态后重试`);
    await saveResponse(response, `aba-${report.direction}-${report.week_end}.csv`);
  },
  async downloadQueryXlsx(
    run: InputRun,
    report: ExpansionReport,
    queryIndex: number,
    query: string,
  ) {
    const response = await fetch(
      `${base(run)}/${encodeURIComponent(report.id)}/queries/${queryIndex}/export.xlsx`,
    );
    if (!response.ok)
      throw new Error(`ABA Excel 下载失败（${response.status}），请查看该查询的采集状态`);
    await saveResponse(response, safeQueryFilename(report.direction, query));
  },
};

export type CollectionMethod = 'keyword_mining' | 'keyword_reverse' | 'traffic_extension';
export type VariantScope = 'all' | 'best_selling' | 'current';

export interface BrowserJob {
  id: string;
  request: {
    direction: 'forward' | 'reverse';
    query: string;
    method: CollectionMethod;
    queries: string[];
    variant_scope: VariantScope | null;
    period?: 'last_30_days';
    month?: string;
  };
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  stage: string;
  error: string;
  report_id: string | null;
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const strings = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((item) => typeof item === 'string');

export function normalizeBrowserJobs(value: unknown): BrowserJob[] {
  if (!Array.isArray(value)) throw new Error('卖家精灵采集任务格式不正确');
  return value.map((item: unknown) => {
    if (
      !record(item) ||
      typeof item.id !== 'string' ||
      !record(item.request) ||
      !['forward', 'reverse'].includes(String(item.request.direction)) ||
      typeof item.request.query !== 'string' ||
      !['queued', 'running', 'succeeded', 'failed'].includes(String(item.status)) ||
      typeof item.stage !== 'string' ||
      typeof item.error !== 'string' ||
      (item.report_id !== null && typeof item.report_id !== 'string')
    )
      throw new Error('卖家精灵采集任务格式不正确');

    const direction = item.request.direction as 'forward' | 'reverse';
    const query = item.request.query;
    const legacy = typeof item.request.month === 'string' && item.request.method === undefined;
    const method = legacy
      ? direction === 'forward'
        ? 'keyword_mining'
        : 'keyword_reverse'
      : item.request.method;
    const queries = legacy ? [query] : item.request.queries;
    const variantScope = legacy ? null : item.request.variant_scope;
    if (
      !['keyword_mining', 'keyword_reverse', 'traffic_extension'].includes(String(method)) ||
      !strings(queries) ||
      (variantScope !== null &&
        !['all', 'best_selling', 'current'].includes(String(variantScope))) ||
      (!legacy && item.request.period !== 'last_30_days')
    )
      throw new Error('卖家精灵采集任务格式不正确');

    return {
      id: item.id,
      request: {
        direction,
        query,
        method: method as CollectionMethod,
        queries,
        variant_scope: variantScope as VariantScope | null,
        ...(legacy ? { month: item.request.month as string } : { period: 'last_30_days' as const }),
      },
      status: item.status as BrowserJob['status'],
      stage: item.stage,
      error: item.error,
      report_id: item.report_id as string | null,
    };
  });
}

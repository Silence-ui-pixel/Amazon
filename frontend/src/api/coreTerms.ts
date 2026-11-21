/** 第二步接口契约：恢复只读，采集、模型分析和确认均由用户显式触发。 */
import { ApiError, request, type InputRun, type RootCandidates } from './images';
import type { ProductProfile } from './productProfiles';
import type { ProductComparison } from './productTermAnalysis';
export interface AbaReport {
  id: string;
  week_start: string;
  week_end: string;
  query_mode: 'root' | 'asin' | 'product_term';
  roots_snapshot: string[];
  stale: boolean;
  warnings: string[];
  reports: Array<{ query: string; total: number; fetched: number; truncated: boolean }>;
  rule_version?: 'legacy-root-v1' | 'hot-search-core-v1' | 'hot-search-core-v2';
}
export interface CoreCandidate {
  id: string;
  keyword: string;
  search_frequency_rank: number;
  matched_roots: string[];
  root_reference: boolean;
}
export interface CoreTerm extends CoreCandidate {
  keyword_role?: 'core' | 'long_tail';
  translation: string;
  relevance: string;
  intent: string;
  category: string;
  reason: string;
  recommended: boolean;
}
export interface CoreCandidates {
  total: number;
  source_partial: boolean;
  items: CoreCandidate[];
}
export interface CoreAnalysis {
  rule_version?: 'core-terms-v1';
  screened_candidates?: number;
  relevant_candidates?: number;
  id: string;
  report_id: string;
  status: 'running' | 'succeeded' | 'failed' | 'outcome_pending';
  model: string;
  request: { offset: number; limit: number; own_brands: string[] };
  stale: boolean;
  items: CoreTerm[];
  warnings: string[];
  error_code: string | null;
  source_analysis_ids?: string[];
}
export interface ExactIntentEvidence {
  object_match: boolean;
  purpose_match: boolean;
  product_level_match: boolean;
  modifiers_compatible: boolean;
  category: 'exact' | 'broad' | 'accessory' | 'unrelated';
  reasons: Record<
    'object_match' | 'purpose_match' | 'product_level_match' | 'modifiers_compatible',
    string
  >;
  explicit_conflict: boolean;
  accessory_target: boolean;
  broad_category: boolean;
}
export interface HotSearchCoreItem {
  candidate_id: string;
  keyword: string;
  search_frequency_rank: number;
  source_product_terms: string[];
  brand_category: 'generic' | 'own_brand' | 'competitor_brand' | 'brand_unknown';
  intent_evidence: ExactIntentEvidence | null;
  summary: {
    valid_samples: number;
    high_related_count: number;
    relevance_percent: string | null;
    confidence: 'none' | 'low' | 'medium' | 'high';
  } | null;
  comparisons: Array<ProductComparison & { high_related: boolean }>;
  status: 'waiting' | 'running' | 'completed' | 'no_samples' | 'failed';
  exclusion_reasons: string[];
}
export interface HotSearchCoreAnalysis {
  id: string;
  project_id: string;
  run_id: string;
  report_id: string;
  fingerprint: string;
  rule_version: 'hot-search-core-v1' | 'hot-search-core-v2';
  profile_version: number;
  profile_snapshot: ProductProfile;
  own_brands_snapshot: string[];
  marketplace: string;
  delivery_region: string;
  input_snapshot: Record<string, unknown>;
  status: 'queued' | 'running' | 'completed' | 'incomplete' | 'failed' | 'stale';
  complete: boolean;
  stale: boolean;
  source_partial: boolean;
  items: HotSearchCoreItem[];
  recommended_candidate_ids: string[];
  batches: Array<{
    id: string;
    kind: 'open' | 'intent' | 'collection' | 'search' | 'product' | 'comparison';
    candidate_ids: string[];
    request: Record<string, unknown>;
    attempt: number;
    status: 'planned' | 'started' | 'completed' | 'invalid' | 'failed' | 'outcome_unknown';
    result: Record<string, unknown> | null;
    error_code: string | null;
    error: string | null;
  }>;
  collections: Record<
    string,
    {
      term: string;
      raw_natural_count: number;
      unique_asins: string[];
      products: unknown[];
      failures: Array<{ asin: string; reason: string }>;
      pages: Array<{
        region: string;
        cards: Array<{ asin: string; image_url: string; position: number }>;
        next_page: boolean;
        url: string;
        screenshot: string;
        diagnostic: string;
      }>;
    }
  >;
  snapshots: Record<string, unknown>;
  snapshot_failures: Record<string, string>;
  created_at: string;
}
export type HotSearchCollectionStatus = 'queued' | 'collecting' | 'completed' | 'failed';
export type HotSearchExportStatus =
  | 'queued'
  | 'downloading'
  | 'parsing'
  | 'verifying'
  | 'completed'
  | 'download_failed'
  | 'parse_failed'
  | 'verification_failed';
export interface HotSearchCollectionTask {
  product_term: string;
  status: HotSearchCollectionStatus;
  page: number;
  candidate_count: number;
  last_output: string;
  error_code: string;
  error: string;
  export_status: HotSearchExportStatus;
  export_error_code: string;
  export_error: string;
  retries: number;
  updated_at: string;
}
export interface HotSearchCollectionJob {
  id: string;
  project_id: string;
  run_id: string;
  fingerprint: string;
  context_hash: string;
  rule_version: 'hot-search-core-v2';
  week_start: string;
  week_end: string;
  status: HotSearchCollectionStatus;
  report_id: string | null;
  tasks: HotSearchCollectionTask[];
  created_at: string;
  updated_at: string;
  stale: boolean;
}
export type AnyCoreAnalysis = CoreAnalysis | HotSearchCoreAnalysis;
export const isHotAnalysis = (value: AnyCoreAnalysis): value is HotSearchCoreAnalysis =>
  value.rule_version === 'hot-search-core-v1' || value.rule_version === 'hot-search-core-v2';
export interface CoreSelection {
  rule_version?: 'core-terms-v1' | 'hot-search-core-v1' | 'hot-search-core-v2';
  id: string;
  analysis_id: string;
  report_id: string;
  version: number;
  stale: boolean;
  items: CoreTerm[];
}
const base = (run: InputRun) => `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}`;
const json = (method: string, data: unknown) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(data),
});
export const coreApi = {
  roots: (run: InputRun) => request<RootCandidates>(`${base(run)}/root-candidates`),
  reports: (run: InputRun) => request<AbaReport[]>(`${base(run)}/aba-reports?limit=20`),
  report: (run: InputRun, id: string) => request<AbaReport>(`${base(run)}/aba-reports/${id}`),
  collect: (run: InputRun, week: string, mode: 'root' | 'product_term' = 'root') =>
    request<AbaReport>(
      `${base(run)}/aba-reports`,
      json(
        'POST',
        mode === 'product_term'
          ? { query_mode: mode, max_rows: 1000 }
          : { week_end: week, query_mode: mode, max_rows: 1000 },
      ),
    ),
  candidates: (run: InputRun, id: string, offset: number, limit = 20) =>
    request<CoreCandidates>(
      `${base(run)}/aba-reports/${id}/core-candidates?offset=${offset}&limit=${limit}`,
    ),
  hotCandidates: (run: InputRun, id: string) =>
    request<HotSearchCoreItem[]>(`${base(run)}/aba-reports/${id}/hot-search-candidates`),
  analyses: (run: InputRun, id: string) =>
    request<CoreAnalysis[]>(`${base(run)}/aba-reports/${id}/core-analyses`),
  analysis: (run: InputRun, id: string) =>
    request<CoreAnalysis>(`${base(run)}/core-analyses/${id}`),
  analyze: (run: InputRun, id: string, offset: number, limit: number, brands: string[]) =>
    request<CoreAnalysis>(
      `${base(run)}/aba-reports/${id}/core-analyses`,
      json('POST', { offset, limit, own_brands: brands }),
    ),
  async hotLatest(run: InputRun): Promise<HotSearchCoreAnalysis | null> {
    try {
      return await request<HotSearchCoreAnalysis>(`${base(run)}/hot-search-core-analysis`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  },
  hotAnalysis: (run: InputRun, id: string) =>
    request<HotSearchCoreAnalysis>(`${base(run)}/hot-search-core-analyses/${id}`),
  executeHot: (run: InputRun, id: string) =>
    request<HotSearchCoreAnalysis>(`${base(run)}/hot-search-core-analyses/${id}/execute`, {
      method: 'POST',
    }),
  analyzeHot: (run: InputRun, id: string, brands: string[]) =>
    request<HotSearchCoreAnalysis>(
      `${base(run)}/hot-search-core-analyses`,
      json('POST', { report_id: id, own_brands: brands }),
    ),
  async hotCollectionLatest(run: InputRun): Promise<HotSearchCollectionJob | null> {
    try {
      return await request<HotSearchCollectionJob>(`${base(run)}/hot-search-collections/latest`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  },
  startHotCollection: (run: InputRun) =>
    request<HotSearchCollectionJob>(`${base(run)}/hot-search-collections`, { method: 'POST' }),
  hotCollection: (run: InputRun, id: string) =>
    request<HotSearchCollectionJob>(`${base(run)}/hot-search-collections/${id}`),
  retryHotCollectionTerm: (run: InputRun, id: string, productTerm: string) =>
    request<HotSearchCollectionJob>(
      `${base(run)}/hot-search-collections/${id}/retry`,
      json('POST', { product_term: productTerm }),
    ),
  hotCollectionWorkbookUrl: (run: InputRun, id: string) =>
    `${base(run)}/hot-search-collections/${id}/workbook.xlsx`,
  async selection(run: InputRun): Promise<CoreSelection | null> {
    try {
      return await request<CoreSelection>(`${base(run)}/core-selection`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  },
  save: (
    run: InputRun,
    analysis: string,
    ids: string[],
    version: number,
    rule: 'core-terms-v1' | 'hot-search-core-v1' | 'hot-search-core-v2' = 'core-terms-v1',
  ) =>
    request<CoreSelection>(
      `${base(run)}/core-selection`,
      json('PUT', {
        analysis_id: analysis,
        candidate_ids: ids,
        expected_version: version,
        rule_version: rule,
      }),
    ),
};

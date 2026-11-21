import { ApiError, request, type InputRun } from './images';

export interface ProductComparison {
  asin: string;
  status: 'high_related' | 'low_related' | 'excluded';
  purpose_score: number | null;
  style_structure_score: number | null;
  specifications_score: number | null;
  price_score: string | null;
  overall_score: string | null;
  exclusion_reason: string;
  natural_position: number | null;
  purpose_reason: string;
  style_structure_reason: string;
  specifications_reason: string;
  price_reason: string;
  candidate_profile: null | {
    purpose: string[];
    style_structure: string[];
    specifications: string[];
    price: string | null;
    currency: string | null;
  };
}
export interface ProductTermAnalysis {
  id: string;
  status: 'queued' | 'running' | 'completed' | 'incomplete' | 'failed' | 'stale' | 'awaiting_review';
  candidate_version?: number;
  complete: boolean;
  best_candidate_ids: string[];
  items: Array<{
    candidate_id: string;
    text: string;
    status: 'waiting' | 'running' | 'completed' | 'no_samples' | 'failed';
    summary: null | {
      valid_samples: number;
      high_related_count: number;
      relevance_percent: string | null;
      confidence: 'none' | 'low' | 'medium' | 'high';
    };
    comparisons: ProductComparison[];
    error: string;
    phase?: 'waiting' | 'searching' | 'collecting' | 'scoring' | 'completed' | 'failed';
    search_pages?: number;
    found_products?: number;
    collected_products?: number;
    collection_failures?: number;
    scored_products?: number;
    rejection_reason?: string;
    evidence_loaded?: boolean;
    updated_at?: string;
    collection: null | {
      raw_natural_count: number;
      unique_asins: string[];
      failures: Array<{ asin: string; reason: string }>;
      pages: Array<{ url: string; screenshot: string; diagnostic: string }>;
    };
  }>;
}
const base = (run: InputRun) =>
  `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/product-term-analyses`;
export function startProductTermAnalysis(
  run: InputRun,
  refresh = false,
): Promise<ProductTermAnalysis> {
  return request<ProductTermAnalysis>(base(run) + (refresh ? '?refresh=true' : ''), {
    method: 'POST',
  });
}
export async function readProductTermAnalysis(run: InputRun): Promise<ProductTermAnalysis | null> {
  try { return await request<ProductTermAnalysis>(base(run) + '/latest?summary=true'); }
  catch (error) { if (error instanceof ApiError && error.status === 404) return null; throw error; }
}

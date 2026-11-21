import { request, type InputRun } from './images';

export interface VerificationItem {
  candidate_id: string;
  text: string;
  translation: string;
  active_query?: string;
  matched_query?: string;
  expansions?: string[];
  completed_queries?: string[];
  status: string;
  page: number | null;
  position: number | null;
  checked_pages: number;
  matched_asin: string | null;
  match_method: 'asin' | 'main_image' | null;
  search_url: string;
  evidence_url: string;
  error_code?: string;
  failure_stage?: string;
  diagnostic_url?: string;
  region: string;
  message: string;
}
export interface Verification {
  id: string;
  reference_image_id: string | null;
  status: string;
  region: string;
  items: VerificationItem[];
  best_candidate_ids: string[];
  final: boolean;
  selection_saved: boolean;
  model_outcome_pending?: boolean;
  review_status?: string;
  message: string;
}
const base = (run: InputRun) =>
  `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/root-verifications`;
export function startVerification(
  run: InputRun,
  referenceImageId: string,
  candidateIds: string[],
): Promise<Verification> {
  return request<Verification>(base(run), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      max_pages: 3,
      reference_image_id: referenceImageId || null,
      candidate_ids: candidateIds,
    }),
  });
}
export function readVerification(run: InputRun, id: string): Promise<Verification> {
  return request<Verification>(`${base(run)}/${id}`);
}

/** 操作只针对原核验任务，保留已完成页面。 */
export function verificationAction(
  run: InputRun,
  id: string,
  action: 'resume' | 'review',
): Promise<Verification> {
  return request<Verification>(`${base(run)}/${id}/${action}`, { method: 'POST' });
}

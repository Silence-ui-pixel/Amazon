import { request } from './images';

export interface ResearchRecord {
  id: string;
  project_id: string;
  asin: string | null;
  marketplace: string;
  product_name: string;
  project_name: string;
  created_at: string;
  updated_at: string;
  status: string;
  workflow_id: string | null;
  report_week: string | null;
  current_step: string;
  error: string;
  library: { id: string; publication_version: number | null; total: number; ranked: number } | null;
}
export interface ResearchPage {
  total: number;
  offset: number;
  limit: number;
  items: ResearchRecord[];
}

export function readResearchRecords(q: string, status: string, offset: number, deleted = false) {
  const params = new URLSearchParams({ q, offset: String(offset), limit: '20' });
  if (status) params.set('status', status);
  params.set('deleted', String(deleted));
  return request<ResearchPage>(`/api/v1/research-records?${params}`);
}

export function changeResearchDeleted(row: ResearchRecord, deleted: boolean) {
  const url = `/api/v1/projects/${row.project_id}/research-records/${row.id}`;
  return request(deleted ? url : `${url}/restore`, { method: deleted ? 'DELETE' : 'POST' });
}

export function researchLink(row: Pick<ResearchRecord, 'id' | 'project_id'>) {
  return `/?${new URLSearchParams({ project: row.project_id, run: row.id, history: '1' })}`;
}

/** 先恢复实际保存的报告周，再挂载按周读取的业务面板。 */
export async function readResearchWeek(row: { id: string; project_id: string }) {
  const reports = await request<Array<{ week_end: string }>>(
    `/api/v1/projects/${row.project_id}/pipeline-runs/${row.id}/aba-reports?limit=1`,
  );
  return reports[0]?.week_end;
}

export function researchExport(row: ResearchRecord) {
  return row.library
    ? `/api/v1/projects/${row.project_id}/pipeline-runs/${row.id}/library/${row.library.id}/template.xlsx?ranked_only=true`
    : '';
}

export const researchStatuses: Record<string, string> = {
  input_received: '已保存，未自动运行',
  queued: '运行已提交',
  running: '运行中',
  waiting_human: '等待人工处理',
  waiting_external: '等待采集',
  completed: '已完成',
  failed: '运行失败',
  interrupted: '运行中断',
  aborted: '已终止',
  superseded: '已被新流程替代',
  incompatible: '需要重新启动',
};

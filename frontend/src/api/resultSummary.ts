import { request, type InputRun, type RootSelection } from './images';
import type { WorkflowView } from './workflow';
import type { StepStatus } from '../workflowGuide';

export interface ResultSummary {
  run_id: string;
  workflow: WorkflowView | null;
  steps: StepStatus[];
  core_selection_id: string;
  competitor_selection_id: string;
  root_selection: RootSelection | null;
  report_week: string | null;
  result_version: string;
}
export function readResultSummary(run: InputRun): Promise<ResultSummary> {
  return request(`/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/summary`);
}

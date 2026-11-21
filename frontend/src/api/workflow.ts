import { ApiError, request, type InputRun } from './images';

export type WorkflowStatus =
  | 'queued'
  | 'running'
  | 'waiting_human'
  | 'waiting_external'
  | 'completed'
  | 'failed'
  | 'interrupted'
  | 'superseded'
  | 'aborted'
  | 'incompatible';
export type WorkflowNodeStatus =
  'pending' | 'running' | 'waiting_human' | 'waiting_external' | 'done' | 'failed' | 'skipped';
export type WorkflowEventStatus =
  'running' | 'waiting_human' | 'waiting_external' | 'done' | 'failed' | 'skipped' | 'interrupted';

export interface WorkflowNodeView {
  key: string;
  label: string;
  status: WorkflowNodeStatus;
  detail: string;
}
export interface WorkflowEventView {
  sequence: number;
  node_key: string;
  node_label: string;
  status: WorkflowEventStatus;
  attempt: number;
  message: string;
  input_summary: Record<string, string>;
  output_summary: Record<string, string>;
  error: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
}
export interface WaitingForView {
  kind: 'human' | 'external' | 'stale';
  gate: string;
  title: string;
  instructions: string;
  actions: string[];
  refs: Record<string, string>;
  ready_to_resume: boolean;
}
export interface WorkflowView {
  workflow_id: string;
  run_id: string;
  project_id: string;
  status: WorkflowStatus;
  revision: number;
  current_node: string | null;
  nodes: WorkflowNodeView[];
  events: WorkflowEventView[];
  waiting_for: WaitingForView | null;
  error: string;
  next_poll_ms: number;
  created_at: string;
  updated_at: string;
}
export interface WorkflowOptions {
  reanalyze_core?: boolean;
  manual_cleaning_review?: boolean;
  report_week_end?: string;
  auto_decide?: boolean;
  verify_roots?: boolean;
  own_brands?: string[];
  core_max_pages?: number;
  third_party_mode?: 'manual' | 'auto';
  deadline_hours?: number;
}
export interface ResumeInput {
  gate: string;
  action: 'continue' | 'poll' | 'abort';
  expected_revision: number;
  payload?: Record<string, string>;
}

const object = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const workflowStatuses: WorkflowStatus[] = [
  'queued',
  'running',
  'waiting_human',
  'waiting_external',
  'completed',
  'failed',
  'interrupted',
  'superseded',
  'aborted',
  'incompatible',
];
const nodeStatuses: WorkflowNodeStatus[] = [
  'pending',
  'running',
  'waiting_human',
  'waiting_external',
  'done',
  'failed',
  'skipped',
];
const eventStatuses: WorkflowEventStatus[] = [
  'running',
  'waiting_human',
  'waiting_external',
  'done',
  'failed',
  'skipped',
  'interrupted',
];
function validSummary(value: unknown): value is Record<string, string> {
  return object(value) && Object.values(value).every((item) => typeof item === 'string');
}
function validEvent(v: unknown): v is WorkflowEventView {
  return (
    object(v) &&
    Number.isInteger(v.sequence) &&
    Number.isInteger(v.attempt) &&
    Number(v.sequence) >= 1 &&
    Number(v.attempt) >= 1 &&
    ['node_key', 'node_label', 'message', 'error', 'started_at'].every(
      (key) => typeof v[key] === 'string',
    ) &&
    eventStatuses.includes(v.status as WorkflowEventStatus) &&
    validSummary(v.input_summary) &&
    validSummary(v.output_summary) &&
    (v.finished_at === null || typeof v.finished_at === 'string') &&
    (v.duration_ms === null || (typeof v.duration_ms === 'number' && v.duration_ms >= 0))
  );
}
function validNode(v: unknown): v is WorkflowNodeView {
  return (
    object(v) &&
    ['key', 'label', 'detail'].every((key) => typeof v[key] === 'string') &&
    nodeStatuses.includes(v.status as WorkflowNodeStatus)
  );
}
function validWaiting(v: unknown): v is WaitingForView {
  return (
    object(v) &&
    ['human', 'external', 'stale'].includes(String(v.kind)) &&
    ['gate', 'title', 'instructions'].every((key) => typeof v[key] === 'string') &&
    Array.isArray(v.actions) &&
    v.actions.every((item) => typeof item === 'string') &&
    object(v.refs) &&
    typeof v.ready_to_resume === 'boolean'
  );
}
function validView(v: unknown): v is WorkflowView {
  return (
    object(v) &&
    ['workflow_id', 'run_id', 'project_id', 'created_at', 'updated_at'].every(
      (key) => typeof v[key] === 'string',
    ) &&
    workflowStatuses.includes(v.status as WorkflowStatus) &&
    typeof v.revision === 'number' &&
    (v.current_node === null || typeof v.current_node === 'string') &&
    Array.isArray(v.nodes) &&
    v.nodes.every(validNode) &&
    Array.isArray(v.events) &&
    v.events.every(validEvent) &&
    (v.waiting_for === null || validWaiting(v.waiting_for)) &&
    typeof v.error === 'string' &&
    typeof v.next_poll_ms === 'number'
  );
}

function parse(value: unknown): WorkflowView {
  const normalized = object(value) && !('events' in value) ? { ...value, events: [] } : value;
  if (!validView(normalized)) throw new Error('编排状态格式不正确');
  return normalized;
}

const base = (run: InputRun) =>
  `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/workflow`;

const json = (method: string, data: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(data),
});

export const workflowApi = {
  async latest(run: InputRun): Promise<WorkflowView | null> {
    try {
      return parse(await request<unknown>(base(run)));
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  },
  async start(run: InputRun, options: WorkflowOptions = {}, browserDeviceId: string | null = null) {
    return parse(
      await request<unknown>(
        base(run),
        json('POST', { options, browser_device_id: browserDeviceId }),
      ),
    );
  },
  async resume(run: InputRun, id: string, data: ResumeInput) {
    return parse(
      await request<unknown>(`${base(run)}/${encodeURIComponent(id)}/resume`, json('POST', data)),
    );
  },
  async abort(run: InputRun, id: string) {
    return parse(
      await request<unknown>(`${base(run)}/${encodeURIComponent(id)}/abort`, { method: 'POST' }),
    );
  },
  async retryTraffic(run: InputRun, id: string, revision: number) {
    return parse(
      await request<unknown>(
        `${base(run)}/${encodeURIComponent(id)}/retry-traffic`,
        json('POST', { expected_revision: revision, gate: 'traffic_review', action: 'continue' }),
      ),
    );
  },
  async retryCleaning(run: InputRun, id: string, revision: number) {
    return parse(
      await request<unknown>(
        `${base(run)}/${encodeURIComponent(id)}/retry-cleaning`,
        json('POST', { expected_revision: revision, gate: 'cleaning_review', action: 'continue' }),
      ),
    );
  },
};

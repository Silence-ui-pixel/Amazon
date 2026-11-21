import { request } from './images';

export type BrowserAgentState = 'offline' | 'online' | 'busy' | 'attention_required';

export interface BrowserAgentStatusView {
  state: BrowserAgentState;
  operation: string;
  operation_id: string;
  started_at: string | null;
  last_error_code: string;
  version: string;
  checked_at: string;
}

const isStatus = (value: unknown): value is BrowserAgentStatusView => {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const item = value as Record<string, unknown>;
  return (
    ['offline', 'online', 'busy', 'attention_required'].includes(String(item.state)) &&
    ['operation', 'operation_id', 'last_error_code', 'version', 'checked_at'].every(
      (key) => typeof item[key] === 'string',
    ) &&
    (item.started_at === null || typeof item.started_at === 'string')
  );
};

const parse = (value: unknown) => {
  if (!isStatus(value)) throw new Error('采集端状态格式不正确');
  return value;
};

export const browserAgentApi = {
  async get() {
    return parse(await request<unknown>('/api/v1/browser-agent/status'));
  },
  async refresh() {
    return parse(
      await request<unknown>('/api/v1/browser-agent/status/refresh', { method: 'POST' }),
    );
  },
};

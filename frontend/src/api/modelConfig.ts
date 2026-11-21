export interface ModelEntry {
  id: string;
  name: string;
  context: string;
  output: string;
}

export type ModelProtocol = 'openai-chat' | 'anthropic' | 'openai-responses';

export interface ProviderSummary {
  provider_id: string;
  name: string;
  base_url: string;
  protocol: ModelProtocol | string;
  model: string;
  output_limit?: number;
  has_key?: boolean;
  models?: ModelEntry[];
}

export interface ModelConfigView {
  provider_id: string;
  name: string;
  base_url: string;
  protocol: ModelProtocol | string;
  model: string;
  vision_model: string;
  output_limit: number;
  api_key_masked: string;
  has_api_key: boolean;
  models: ModelEntry[];
  presets: ModelEntry[];
  providers?: ProviderSummary[];
}

export interface ModelConfigUpdate {
  provider_id: string;
  name: string;
  base_url: string;
  protocol: ModelProtocol;
  model: string;
  vision_model: string;
  output_limit: number;
  api_key: string;
  models: ModelEntry[];
}

export interface ModelTestResult {
  ok: boolean;
  message: string;
  latency_ms: number;
  models: string[];
}

const BASE = '/api/v1/model-config';

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(BASE + path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text.slice(0, 240) || `HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export const modelConfigApi = {
  get: () => call<ModelConfigView>('GET', ''),
  save: (data: ModelConfigUpdate) => call<ModelConfigView>('PUT', '', data),
  test: (data: ModelConfigUpdate) => call<ModelTestResult>('POST', '/test', data),
  activateProvider: (providerId: string) =>
    call<ModelConfigView>('POST', `/providers/${encodeURIComponent(providerId)}/activate`),
  deleteProvider: (providerId: string) =>
    call<{ providers: ProviderSummary[] }>(
      'DELETE',
      `/providers/${encodeURIComponent(providerId)}`,
    ),
  cloneProvider: (providerId: string) =>
    call<{ providers: ProviderSummary[]; cloned: string }>(
      'POST',
      `/providers/${encodeURIComponent(providerId)}/clone`,
    ),
};

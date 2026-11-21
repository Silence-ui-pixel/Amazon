import { request } from './images';

export type BrowserDeviceState = 'unknown' | 'offline' | 'online' | 'busy' | 'attention_required';

export interface BrowserDevice {
  device_id: string;
  display_name: string;
  agent_url: string;
  owner_id: string;
  status: BrowserDeviceState | string;
  agent_version: string;
  created_at: string;
  updated_at: string;
  last_seen_at: string | null;
  registered: boolean;
}

export interface BrowserDeviceVerifyResult {
  device: BrowserDevice;
  reachable: boolean;
  detail: string;
}

export const browserDevicesApi = {
  list(): Promise<BrowserDevice[]> {
    return request<BrowserDevice[]>('/api/v1/browser/devices');
  },
  register(payload: {
    display_name: string;
    agent_url: string;
    agent_token: string;
  }): Promise<BrowserDeviceVerifyResult> {
    return request<BrowserDeviceVerifyResult>('/api/v1/browser/devices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  },
  verify(deviceId: string): Promise<BrowserDeviceVerifyResult> {
    return request<BrowserDeviceVerifyResult>(
      `/api/v1/browser/devices/${encodeURIComponent(deviceId)}/verify`,
      { method: 'POST' },
    );
  },
  remove(deviceId: string): Promise<{ ok: boolean }> {
    return request<{ ok: boolean }>(
      `/api/v1/browser/devices/${encodeURIComponent(deviceId)}`,
      { method: 'DELETE' },
    );
  },
};

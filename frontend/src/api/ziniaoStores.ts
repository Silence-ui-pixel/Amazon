import { getDeviceId } from '../localAgent';

export interface ZiniaoStore {
  id: string;
  name: string;
  platform: string;
}

export interface StoreListResult {
  stores: ZiniaoStore[];
  current?: string;
  error?: string;
  message?: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text.slice(0, 240) || `HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

/** 列出访问者电脑上的紫鸟店铺；deviceId 缺省用本机已配对的设备。 */
export async function listZiniaoStores(deviceId?: string | null): Promise<StoreListResult> {
  const id = deviceId ?? getDeviceId();
  if (!id) throw new Error('当前浏览器还没有配对采集设备');
  return request<StoreListResult>(`/api/v1/browser/devices/${encodeURIComponent(id)}/stores`);
}

/** 在访问者电脑上选定紫鸟店铺。 */
export async function selectZiniaoStore(
  storeId: string,
  deviceId?: string | null,
): Promise<{ ok: boolean; store_id: string }> {
  const id = deviceId ?? getDeviceId();
  if (!id) throw new Error('当前浏览器还没有配对采集设备');
  return request<{ ok: boolean; store_id: string }>(
    `/api/v1/browser/devices/${encodeURIComponent(id)}/stores`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ store_id: storeId }),
    },
  );
}

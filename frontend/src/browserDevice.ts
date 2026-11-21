import { ref } from 'vue';
import { detectedLocalDeviceId, getDeviceId } from './localAgent';

const STORAGE_KEY = 'keyword.agent.selectedDevice';

/**
 * 本次研究用哪台电脑采集。
 *
 * 空值表示「跟随本机」：启动任务时自动带上本机已配对采集器的设备号；
 * 用户也可以在页面上显式指定另一台已登记的电脑（例如用一台常开机的机器采集）。
 */
const selectedDeviceId = ref<string | null>(readStored());

function readStored(): string | null {
  try {
    if (typeof window === 'undefined') return null;
    return window.localStorage.getItem(STORAGE_KEY) || null;
  } catch {
    return null;
  }
}

export function selectedBrowserDeviceId(): string | null {
  return selectedDeviceId.value;
}

export function setSelectedBrowserDeviceId(deviceId: string | null): void {
  selectedDeviceId.value = deviceId || null;
  try {
    if (deviceId) window.localStorage.setItem(STORAGE_KEY, deviceId);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // 隐私模式下 localStorage 不可用；页面仍可用本次会话的选择。
  }
}

/**
 * 启动任务时真正发给服务器的设备号。
 * 优先用户显式选择；其次本机采集运行时刚自报的设备号（比 localStorage 新鲜）；
 * 都没有则留空，由服务器按在线情况兜底。
 */
export function resolveBrowserDeviceId(): string | null {
  return selectedDeviceId.value || detectedLocalDeviceId() || getDeviceId() || null;
}

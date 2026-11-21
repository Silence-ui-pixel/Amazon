const AGENT_URL = 'http://127.0.0.1:7100';
const DEVICE_KEY = 'keyword.agent.deviceId';
const CODE_KEY = 'keyword.agent.pairingCode';

export interface LocalAgentStatus {
  state: 'offline' | 'online' | 'busy' | 'attention_required';
  version: string;
  message: string;
  deviceId?: string;
  displayName?: string;
  /** 本机 127.0.0.1:7100 采集运行时是否就绪（真正能跑采集）。 */
  runtimeReady?: boolean;
  currentVersion?: string;
  updateRequired?: boolean;
}

interface PairingState {
  paired?: boolean;
  device_id?: string;
  channel_connected?: boolean;
  display_name?: string;
  /** 旧版共用 channel://client 设备，必须重新配对，否则多人会串设备。 */
  stale?: boolean;
}

/** 当前浏览器配到的采集设备；启动工作流时要带给服务器。 */
/** 本机采集运行时最近一次自报的设备号；比 localStorage 里的旧值新鲜。 */
let detectedDeviceId = '';

export function detectedLocalDeviceId(): string {
  return detectedDeviceId;
}

export function getDeviceId(): string | null {
  try {
    return window.localStorage.getItem(DEVICE_KEY) || null;
  } catch {
    return null;
  }
}

export function rememberPairingCode(code: string): void {
  try {
    window.localStorage.setItem(CODE_KEY, code);
  } catch {
    // 隐私模式下 localStorage 不可用，页面仍可用配对码查询本次状态。
  }
}

export function clearDevice(): void {
  try {
    window.localStorage.removeItem(DEVICE_KEY);
    window.localStorage.removeItem(CODE_KEY);
  } catch {
    // ignore
  }
}

function saveDeviceId(id: string): void {
  try {
    window.localStorage.setItem(DEVICE_KEY, id);
  } catch {
    // ignore
  }
}

function withTimeout(ms: number): AbortController {
  const controller = new AbortController();
  window.setTimeout(() => controller.abort(), ms);
  return controller;
}

interface RuntimeStatus {
  state: string;
  version: string;
  deviceId: string;
}

async function probeRuntime(): Promise<RuntimeStatus | null> {
  const controller = withTimeout(1500);
  try {
    const response = await fetch(`${AGENT_URL}/v1/status`, {
      signal: controller.signal,
      cache: 'no-store',
    });
    if (!response.ok) return null;
    const data = (await response.json()) as {
      state?: string;
      version?: string;
      device_id?: string;
    };
    return {
      state: data.state ?? 'online',
      version: data.version ?? '',
      deviceId: (data.device_id ?? '').trim(),
    };
  } catch {
    return null;
  }
}

async function queryVersion(deviceId: string): Promise<{ running: string; current: string } | null> {
  try {
    const response = await fetch(`/api/v1/browser/devices/${encodeURIComponent(deviceId)}/version`, {
      signal: withTimeout(4000).signal,
      cache: 'no-store',
    });
    if (!response.ok) return null;
    return (await response.json()) as { running: string; current: string };
  } catch {
    return null;
  }
}

async function probeChannelRuntime(deviceId: string): Promise<RuntimeStatus | null> {
  try {
    const response = await fetch(`/api/v1/agent/channel-probe?device_id=${encodeURIComponent(deviceId)}`, {
      signal: withTimeout(4000).signal,
      cache: 'no-store',
    });
    if (!response.ok) return null;
    const data = (await response.json()) as {
      ok?: boolean;
      status?: { state?: string; version?: string };
    };
    if (!data.ok || !data.status) return null;
    return { state: data.status.state ?? 'online', version: data.status.version ?? '', deviceId };
  } catch {
    return null;
  }
}

function olderVersion(running: string, current: string): boolean {
  const parse = (value: string) => /^v?(\d+)\.(\d+)\.(\d+)$/.exec(value.trim());
  const left = parse(running);
  const right = parse(current);
  if (!left || !right) return false;
  for (let index = 1; index <= 3; index++) {
    const difference = Number(left[index]) - Number(right[index]);
    if (difference) return difference < 0;
  }
  return false;
}

async function queryPairing(query: string): Promise<PairingState | null> {
  const controller = withTimeout(4000);
  try {
    const response = await fetch(`/api/v1/agent/pairing-status?${query}`, {
      signal: controller.signal,
      cache: 'no-store',
    });
    if (!response.ok) return null;
    return (await response.json()) as PairingState;
  } catch {
    return null;
  }
}

/**
 * 判断「访问者自己的电脑」是否连上，优先级：
 * 1. 已保存的 device_id（上次配对过）
 * 2. 本次页面刚生成的配对码（别人在采集器里填完后自动变成已连接）
 * 3. 都没有 → 未安装 / 未配对
 * 本机 7100 只用来判断采集运行时是否就绪，不再作为「是否连接」的唯一依据。
 */
export async function detectLocalAgent(): Promise<LocalAgentStatus> {
  const savedId = getDeviceId();
  let pendingCode = '';
  try {
    pendingCode = window.localStorage.getItem(CODE_KEY) || '';
  } catch {
    pendingCode = '';
  }

  const runtime = await probeRuntime();
  // 本机 7100 直接上报配对到的设备号：清了浏览器缓存也能自动找回自己的设备。
  const runtimeDeviceId = runtime?.deviceId ?? '';
  if (runtimeDeviceId && runtimeDeviceId !== savedId) saveDeviceId(runtimeDeviceId);
  detectedDeviceId = runtimeDeviceId;
  // 本机运行时自报的设备号最准；探测不到才退回浏览器里存的那个。
  const effectiveId = runtimeDeviceId || savedId;
  const query = effectiveId
    ? `device_id=${encodeURIComponent(effectiveId)}`
    : pendingCode
      ? `code=${encodeURIComponent(pendingCode)}`
      : '';
  const pairing = query ? await queryPairing(query) : null;

  if (pairing?.stale) {
    // 旧版所有电脑共用一个 device_id，凭据会互相覆盖；清掉后引导重新配对。
    clearDevice();
    return {
      state: 'attention_required',
      version: runtime?.version ?? '',
      message: '当前采集器是旧版共用设备，请重新下载一键安装包、双击 install.cmd，再生成新的配对码重新配对。',
      runtimeReady: runtime !== null,
    };
  }

  const deviceId = (pairing?.device_id || effectiveId || '').trim();
  if (deviceId && deviceId !== savedId) saveDeviceId(deviceId);
  const name = (pairing?.display_name || '').trim();
  const who = name ? `「${name}」` : '这台电脑的采集端';

  if (pairing?.channel_connected) {
    // 公网页面可能被浏览器禁止探测本机；只查询已配对设备的通道，不换用别人的设备。
    const [release, channelRuntime] = await Promise.all([
      queryVersion(deviceId),
      runtime?.version ? Promise.resolve(null) : probeChannelRuntime(deviceId),
    ]);
    const actual = runtime?.version ? runtime : (channelRuntime ?? runtime);
    const version = actual?.version || release?.running || '';
    const currentVersion = release?.current ?? '';
    const runtimeReady = actual !== null;
    const updateRequired = olderVersion(version, currentVersion);
    if (updateRequired) {
      return {
        state: 'attention_required',
        version,
        currentVersion,
        updateRequired,
        message: `已连接${who}，采集器版本过旧（v${version}，最新 v${currentVersion}）。请点击“下载/重装采集器”，完整解压后运行 install.cmd，再点击“重新检测”。`,
        deviceId,
        displayName: name,
        runtimeReady,
      };
    }
    const state = actual?.state === 'busy' ? 'busy' : 'online';
    return {
      state,
      version,
      currentVersion,
      updateRequired,
      message: runtimeReady
        ? `已连接${who}${state === 'busy' ? '，正在采集' : '，采集端在线'}`
        : `已连接${who}，但本机采集运行时未就绪，请更新到最新版采集器`,
      deviceId,
      displayName: name,
      runtimeReady,
    };
  }

  const version = runtime?.version ?? '';
  const runtimeReady = runtime !== null;
  if (pairing?.paired) {
    return {
      state: 'attention_required',
      version,
      message: `${who}已配对，但采集端没有连上服务器；请确认程序正在运行，或重新双击 install.cmd`,
      deviceId,
      displayName: name,
      runtimeReady,
    };
  }

  if (runtimeReady) {
    // 本机有旧版 Agent（隧道模式）：仍然算在线，但没有配对设备，任务会走服务器默认通道。
    return {
      state: 'online',
      version,
      message: `已检测到${who}（本机运行时），建议重新配对以便任务路由到这台电脑`,
      deviceId,
      displayName: name,
      runtimeReady,
    };
  }

  return {
    state: 'offline',
    version: '',
    message: '未检测到你电脑上的采集端。请下载安装包并双击 install.cmd，首次启动填入页面上的配对码。',
    deviceId,
    displayName: name,
    runtimeReady: false,
  };
}

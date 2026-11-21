<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import {
  clearDevice,
  detectLocalAgent,
  getDeviceId,
  rememberPairingCode,
  type LocalAgentStatus,
} from '../localAgent';

const status = ref<LocalAgentStatus | null>(null);
const busy = ref(false);
const pairingCode = ref('');
const pairingBusy = ref(false);
const error = ref('');
let pollTimer: number | undefined;

const checking = computed(() => status.value === null);
const online = computed(
  () => status.value?.state === 'online' || status.value?.state === 'busy',
);
const pairedButOffline = computed(() => status.value?.state === 'attention_required');
const runtimeMissing = computed(() => online.value && status.value?.runtimeReady === false);
const label = computed(() => {
  if (checking.value) return '正在检测本机采集端';
  if (online.value) return status.value?.state === 'busy' ? '正在采集' : '采集端已连接';
  if (pairedButOffline.value) return '已配对，等待连接';
  return '下载采集器';
});

async function load() {
  busy.value = true;
  error.value = '';
  try {
    status.value = await detectLocalAgent();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '采集端检测失败';
    status.value = { state: 'offline', version: '', message: '' };
  } finally {
    busy.value = false;
  }
}

async function issueCode() {
  pairingBusy.value = true;
  error.value = '';
  try {
    const response = await fetch('/api/v1/agent/pairing-codes', { method: 'POST' });
    if (!response.ok) throw new Error('配对码生成失败，请刷新页面重试');
    const data = (await response.json()) as { code?: string };
    const code = data.code ?? '';
    if (!code) throw new Error('服务器没有返回配对码');
    clearDevice();
    pairingCode.value = code;
    rememberPairingCode(code);
    try {
      await navigator.clipboard.writeText(code);
    } catch {
      // Clipboard permission may be unavailable; the code remains visible for manual entry.
    }
    await load();
    // 生成新码意味着重新配对：提示用户在采集器窗口填入。
    window.setTimeout(() => void load(), 4000);
    window.setTimeout(() => void load(), 9000);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '配对码生成失败';
  } finally {
    pairingBusy.value = false;
  }
}

function stopPolling() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
}

function startPolling() {
  stopPolling();
  // 用户填完配对码后，页面自动变成已连接，不需要手动点「重新检测」。
  pollTimer = window.setInterval(() => {
    if (!checking.value && (pairingCode.value || getDeviceId())) void load();
  }, 3000);
}

onMounted(() => {
  void load().then(startPolling);
});
onBeforeUnmount(stopPolling);
</script>

<template>
  <section class="agent-card" :data-state="checking ? 'checking' : online ? 'online' : 'offline'" aria-label="本机采集端状态">
    <div>
      <span class="dot" aria-hidden="true"></span>
      <strong>{{ label }}</strong>
      <span v-if="status?.displayName">{{ status.displayName }}</span>
      <span v-if="status?.version">v{{ status.version }}</span>
    </div>
    <p v-if="status?.message && !checking">{{ status.message }}</p>
    <p v-else-if="!online && !checking">
      未检测到你电脑上的采集端。请下载安装包、解压到普通文件夹后双击 install.cmd 完成安装并启动，首次启动填入配对码即可连接自己的紫鸟；之后版本会自动升级。
    </p>
    <p v-if="runtimeMissing" class="warn" role="status">
      采集通道已连上，但本机采集运行时未就绪；请重新双击 install.cmd 安装最新版，或在采集器启动后点「重新检测」。
    </p>
    <p v-if="error" role="alert">{{ error }}</p>
    <div class="actions">
      <template v-if="!online && !checking">
        <a class="download" href="/agent/KeywordLibraryAgent.zip" download="KeywordLibraryAgent.zip">下载采集器（一键安装包）</a>
        <a class="alt" href="/agent/KeywordLibraryAgent.exe" download="KeywordLibraryAgent.exe">单文件 exe</a>
        <button type="button" :disabled="pairingBusy" @click="issueCode">
          {{ pairingCode ? '刷新配对码' : '生成配对码' }}
        </button>
        <code v-if="pairingCode" class="code">{{ pairingCode }}</code>
      </template>
      <button type="button" :disabled="busy" @click="load">重新检测</button>
    </div>
  </section>
</template>

<style scoped>
.agent-card { display: flex; align-items: center; gap: 12px; justify-content: space-between; padding: 12px 14px; border: 1px solid #dbe5f0; border-radius: 10px; background: #fff; }
.agent-card > div { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.agent-card span, .agent-card p { color: #64748b; font-size: 12px; }
.agent-card p { margin: 0; }
.agent-card p.warn { color: #b45309; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: #94a3b8; }
[data-state='online'] .dot { background: #22c55e; }
[data-state='offline'] .dot { background: #f59e0b; }
.actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
button, .download, .alt { border: 1px solid #cbd5e1; border-radius: 7px; padding: 7px 10px; background: #fff; color: #334155; font-size: 13px; text-decoration: none; cursor: pointer; }
.download { border-color: #0f8f83; background: #0f8f83; color: #fff; font-weight: 600; }
.alt { color: #475569; }
.download:hover { background: #0b7a70; }
.code { padding: 4px 9px; border-radius: 6px; background: #f1f5f9; color: #0f8f83; font-size: 14px; font-weight: 700; letter-spacing: 1px; }
@media (max-width: 780px) { .agent-card { align-items: flex-start; flex-direction: column; } }
</style>

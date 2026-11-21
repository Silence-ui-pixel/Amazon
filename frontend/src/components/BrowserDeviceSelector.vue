<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { browserDevicesApi, type BrowserDevice } from '../api/browserDevices';
import {
  resolveBrowserDeviceId,
  selectedBrowserDeviceId,
  setSelectedBrowserDeviceId,
} from '../browserDevice';

const props = defineProps<{ disabled?: boolean; readonly?: boolean }>();
const selected = computed<string | null>({
  get: () => selectedBrowserDeviceId(),
  set: (value) => setSelectedBrowserDeviceId(value),
});
const devices = ref<BrowserDevice[]>([]);
const loading = ref(false);
const error = ref('');
const showRegister = ref(false);
const displayName = ref('');
const agentUrl = ref('');
const agentToken = ref('');

/** 实际会用到的设备：显式选择优先，否则跟随本机已配对的采集器。 */
const effectiveDevice = computed<string | null>(() => resolveBrowserDeviceId());
const effectiveName = computed(() => {
  const id = effectiveDevice.value;
  if (!id) return '';
  const found = devices.value.find((device) => device.device_id === id);
  return found ? found.display_name : '本机已配对的采集器';
});

const stateLabels: Record<string, string> = {
  unknown: '未检测',
  offline: '离线',
  online: '在线',
  busy: '采集中',
  attention_required: '需人工处理',
};

async function load() {
  loading.value = true;
  error.value = '';
  try {
    devices.value = await browserDevicesApi.list();
    if (selected.value && !devices.value.some((device) => device.device_id === selected.value)) {
      selected.value = null;
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '设备列表读取失败';
  } finally {
    loading.value = false;
  }
}

async function register() {
  if (!displayName.value.trim() || !agentUrl.value.trim() || !agentToken.value.trim()) {
    error.value = '请完整填写设备名称、Agent 地址和 Token';
    return;
  }
  loading.value = true;
  error.value = '';
  try {
    const result = await browserDevicesApi.register({
      display_name: displayName.value,
      agent_url: agentUrl.value,
      agent_token: agentToken.value,
    });
    devices.value = [
      ...devices.value.filter((device) => device.device_id !== result.device.device_id),
      result.device,
    ];
    selected.value = result.device.device_id;
    displayName.value = '';
    agentUrl.value = '';
    agentToken.value = '';
    showRegister.value = false;
    if (!result.reachable) error.value = result.detail;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '设备登记失败';
  } finally {
    loading.value = false;
  }
}

async function verify(device: BrowserDevice) {
  loading.value = true;
  error.value = '';
  try {
    const result = await browserDevicesApi.verify(device.device_id);
    devices.value = devices.value.map((item) =>
      item.device_id === result.device.device_id ? result.device : item,
    );
    if (!result.reachable) error.value = result.detail;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '设备检测失败';
  } finally {
    loading.value = false;
  }
}

onMounted(() => void load());
watch(() => props.disabled, () => undefined);
</script>

<template>
  <section class="device-selector" aria-label="紫鸟浏览器设备">
    <div class="device-row">
      <label for="browser-device">采集电脑</label>
      <select id="browser-device" v-model="selected" :disabled="disabled || readonly || loading">
        <option :value="null">自动（跟随本机已配对的采集器）</option>
        <option v-for="device in devices" :key="device.device_id" :value="device.device_id">
          {{ device.display_name }} · {{ stateLabels[device.status] ?? device.status }}
        </option>
      </select>
      <button type="button" :disabled="disabled || readonly || loading" @click="showRegister = !showRegister">
        {{ showRegister ? '收起登记' : '登记采集电脑' }}
      </button>
      <button
        v-if="selected"
        type="button"
        class="quiet"
        :disabled="disabled || readonly || loading"
        @click="verify(devices.find((device) => device.device_id === selected)!)"
      >
        重新检测
      </button>
    </div>
    <p class="hint">
      本次任务将使用：{{ effectiveName || '尚未确定（启动时会自动选唯一在线的采集器）' }}。
      选择后，本次任务及其恢复都会固定使用该电脑，不会串到别人的紫鸟。
    </p>
    <form v-if="showRegister && !readonly" class="register" @submit.prevent="register">
      <input v-model="displayName" placeholder="设备名称，例如：运营电脑" maxlength="200" />
      <input v-model="agentUrl" placeholder="Agent 地址，例如：http://127.0.0.1:7100" type="url" />
      <input v-model="agentToken" placeholder="Agent Token" type="password" minlength="32" />
      <button type="submit" :disabled="loading">登记并检测</button>
    </form>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.device-selector { display: grid; gap: 7px; padding: 10px 12px; border: 1px solid #dbe5f0; border-radius: 10px; background: #fff; }
.device-row, .register { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
label { font-size: 13px; font-weight: 650; color: #334155; }
select, input { min-height: 32px; border: 1px solid #cbd5e1; border-radius: 6px; padding: 5px 8px; color: #334155; background: #fff; }
select { min-width: 220px; }
input { min-width: 210px; }
button { min-height: 32px; border: 1px solid #cbd5e1; border-radius: 6px; padding: 5px 10px; color: #334155; background: #f8fafc; cursor: pointer; }
button.quiet { background: #fff; }
button:disabled, select:disabled, input:disabled { cursor: not-allowed; opacity: .55; }
.hint, .error { margin: 0; font-size: 12px; color: #64748b; }
.error { color: #b42318; }
@media (max-width: 720px) { select, input { min-width: min(100%, 280px); flex: 1 1 200px; } }
</style>

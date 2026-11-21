<script setup lang="ts">
import { onMounted, ref } from 'vue';
import {
  modelConfigApi,
  type ModelConfigUpdate,
  type ModelConfigView,
  type ModelEntry,
  type ModelProtocol,
} from '../api/modelConfig';

defineEmits<{ close: [] }>();

const PROTOCOLS: { value: ModelProtocol; label: string }[] = [
  { value: 'openai-chat', label: '/chat/completions（OpenAI 兼容格式）' },
  { value: 'anthropic', label: '/messages（Anthropic 格式）' },
  { value: 'openai-responses', label: '/responses（OpenAI 新格式）' },
];

const config = ref<ModelConfigView | null>(null);
const form = ref<ModelConfigUpdate>({
  provider_id: '',
  name: '',
  base_url: '',
  protocol: 'openai-chat',
  model: '',
  vision_model: '',
  output_limit: 20000,
  api_key: '',
  models: [],
});
const busy = ref(false);
const error = ref('');
const notice = ref('');
const testLine = ref('');
const testOk = ref(false);
const adding = ref(false);
const showKey = ref(false);
const draft = ref<ModelEntry>({ id: '', name: '', context: '1M', output: '128K' });
// 接口真实返回的模型列表；点「测试连接」或「刷新模型列表」后填充。
const remoteModels = ref<string[]>([]);
const remoteLoading = ref(false);
const remoteError = ref('');

function sync(data: ModelConfigView): void {
  config.value = data;
  form.value = {
    provider_id: data.provider_id,
    name: data.name,
    base_url: data.base_url,
    protocol: (data.protocol as ModelProtocol) || 'openai-chat',
    model: data.model,
    vision_model: data.vision_model,
    output_limit: data.output_limit,
    api_key: '',
    models: data.models.map((item) => ({ ...item })),
  };
}

function message(errorValue: unknown, fallback: string): string {
  return errorValue instanceof Error ? errorValue.message : fallback;
}

async function load(): Promise<void> {
  busy.value = true;
  error.value = '';
  try {
    sync(await modelConfigApi.get());
  } catch (reason) {
    error.value = message(reason, '读取模型配置失败');
  } finally {
    busy.value = false;
  }
}

async function save(success = '配置已保存并立即生效'): Promise<void> {
  busy.value = true;
  error.value = '';
  notice.value = '';
  try {
    sync(await modelConfigApi.save({ ...form.value, api_key: form.value.api_key.trim() }));
    notice.value = success;
    showKey.value = false;
  } catch (reason) {
    error.value = message(reason, '保存失败');
  } finally {
    busy.value = false;
  }
}

async function applyModel(entry: ModelEntry): Promise<void> {
  if (busy.value) return;
  form.value.model = entry.id;
  form.value.vision_model = entry.id;
  await save(`已切换到 ${entry.id}，后续任务立即生效`);
}

async function runTest(): Promise<void> {
  busy.value = true;
  testLine.value = '正在测试…';
  try {
    const result = await modelConfigApi.test({ ...form.value, api_key: form.value.api_key.trim() });
    testOk.value = result.ok;
    testLine.value = `${result.message} · ${result.latency_ms}ms` +
      (result.models.length ? ` · ${result.models.length} 个模型` : '');
    // 接口能列出模型时，直接把真实清单填进「添加模型」下拉。
    if (result.models.length) {
      remoteModels.value = result.models;
      remoteError.value = '';
    }
  } catch (reason) {
    testOk.value = false;
    testLine.value = message(reason, '测试失败');
  } finally {
    busy.value = false;
  }
}

async function loadRemoteModels(): Promise<void> {
  remoteLoading.value = true;
  remoteError.value = '';
  try {
    const result = await modelConfigApi.test({ ...form.value, api_key: form.value.api_key.trim() });
    remoteModels.value = result.models ?? [];
    if (!result.models.length) {
      remoteError.value = result.ok
        ? '这个接口没有返回模型列表，请手动填写模型 ID'
        : result.message;
    }
    testOk.value = result.ok;
    testLine.value = `${result.message} · ${result.latency_ms}ms`;
  } catch (reason) {
    remoteError.value = message(reason, '读取模型列表失败');
  } finally {
    remoteLoading.value = false;
  }
}

function addModel(): void {
  const id = draft.value.id.trim();
  if (!id) {
    error.value = '请填写模型 ID';
    return;
  }
  if (form.value.models.some((item) => item.id === id)) {
    error.value = '该模型已在列表中';
    return;
  }
  form.value.models.push({
    id,
    name: draft.value.name.trim() || id,
    context: draft.value.context.trim() || '1M',
    output: draft.value.output.trim() || '128K',
  });
  draft.value = { id: '', name: '', context: '1M', output: '128K' };
  adding.value = false;
}

function toggleAdd(): void {
  adding.value = !adding.value;
  if (adding.value && !remoteModels.value.length) void loadRemoteModels();
}

function removeModel(id: string): void {
  if (id === form.value.model) {
    error.value = '不能删除当前使用的模型，请先切换到其他模型';
    return;
  }
  form.value.models = form.value.models.filter((item) => item.id !== id);
}

function choosePreset(id: string): void {
  const hit = config.value?.presets.find((item) => item.id === id);
  if (hit) {
    draft.value.id = hit.id;
    draft.value.name = hit.name;
  }
}

/** 选接口返回的模型时，展示名默认沿用已有条目或模型 ID。 */
function pickRemoteModel(id: string): void {
  draft.value.id = id;
  const known = form.value.models.find((item) => item.id === id);
  draft.value.name = known?.name ?? '';
}

async function activateProvider(providerId: string): Promise<void> {
  if (busy.value || providerId === form.value.provider_id) return;
  busy.value = true;
  error.value = '';
  notice.value = '';
  try {
    sync(await modelConfigApi.activateProvider(providerId));
    notice.value = `已切换到供应商 ${providerId}，后续任务立即生效`;
  } catch (reason) {
    error.value = message(reason, '切换供应商失败');
  } finally {
    busy.value = false;
  }
}

async function cloneProvider(providerId: string): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    const result = await modelConfigApi.cloneProvider(providerId);
    notice.value = `已克隆为 ${result.cloned}，填写 API Key 并保存后即可使用`;
    await load();
  } catch (reason) {
    error.value = message(reason, '克隆供应商失败');
  } finally {
    busy.value = false;
  }
}

async function deleteProvider(providerId: string): Promise<void> {
  if (busy.value) return;
  if (providerId === form.value.provider_id) {
    error.value = '不能删除当前使用的供应商，请先切换到其他供应商';
    return;
  }
  busy.value = true;
  error.value = '';
  try {
    await modelConfigApi.deleteProvider(providerId);
    await load();
  } catch (reason) {
    error.value = message(reason, '删除供应商失败');
  } finally {
    busy.value = false;
  }
}

function protocolLabel(value: string): string {
  return PROTOCOLS.find((p) => p.value === value)?.label ?? value;
}

onMounted(() => void load());
</script>

<template>
  <div class="mask" @click.self="$emit('close')">
    <section class="panel" role="dialog" aria-label="大模型设置">
      <header class="panel-head">
        <div>
          <h2>大模型设置</h2>
          <p>选择模型即一键切换，保存后无需重启服务，后续任务立即生效。</p>
        </div>
        <button type="button" class="close" aria-label="关闭" @click="$emit('close')">×</button>
      </header>

      <div v-if="error" class="alert error">{{ error }}</div>
      <div v-if="notice" class="alert ok">{{ notice }}</div>

      <div v-if="config" class="body">
        <div class="grid">
          <label>供应商 ID
            <input v-model="form.provider_id" maxlength="100" />
          </label>
          <label>名称
            <input v-model="form.name" maxlength="200" />
          </label>
          <label class="wide">接口地址
            <input v-model="form.base_url" placeholder="https://…/v1" />
          </label>
          <label class="wide">接口协议
            <select v-model="form.protocol">
              <option v-for="p in PROTOCOLS" :key="p.value" :value="p.value">{{ p.label }}</option>
            </select>
          </label>
          <label class="wide">API Key
            <div class="key-row">
              <input
                v-model="form.api_key"
                :type="showKey ? 'text' : 'password'"
                :placeholder="config.api_key_masked || '未配置'"
                autocomplete="off"
              />
              <button type="button" class="mini" @click="showKey = !showKey">
                {{ showKey ? '隐藏' : '显示' }}
              </button>
            </div>
            <small>留空表示沿用现有密钥（{{ config.has_api_key ? '已配置' : '未配置' }}）</small>
          </label>
        </div>

        <div class="test-row">
          <span :class="['test-state', testOk ? 'ok' : 'idle']">
            <i></i>{{ testLine || '尚未测试连接' }}
          </span>
          <button type="button" class="mini" :disabled="busy" @click="runTest">测试连接</button>
        </div>

        <div class="section-head">
          <strong>供应商</strong>
          <span>点「切换」即启用；克隆会复制配置但不复制密钥</span>
        </div>
        <div class="provider-list">
          <div
            v-for="item in config.providers || []"
            :key="item.provider_id"
            :class="['provider-row', { active: item.provider_id === form.provider_id }]"
          >
            <div class="provider-main">
              <strong>{{ item.name || item.provider_id }}</strong>
              <small>{{ item.provider_id }} · {{ item.base_url }} · {{ item.model }}</small>
            </div>
            <span class="proto">{{ protocolLabel(item.protocol) }}</span>
            <span v-if="item.provider_id === form.provider_id" class="current">当前</span>
            <span v-else class="provider-actions">
              <button type="button" class="mini" :disabled="busy" @click="activateProvider(item.provider_id)">切换</button>
              <button type="button" class="mini" :disabled="busy" @click="cloneProvider(item.provider_id)">克隆</button>
              <button type="button" class="mini danger" :disabled="busy" @click="deleteProvider(item.provider_id)">删除</button>
            </span>
          </div>
          <p v-if="!(config.providers || []).length" class="empty">
            还没有保存过供应商。填写上面的配置后点「保存并应用」就会登记为第一个供应商。
          </p>
        </div>

        <div class="section-head">
          <strong>模型列表</strong>
          <span>点击模型行即「一键切换」</span>
          <button type="button" class="mini" @click="toggleAdd">
            {{ adding ? '收起' : '＋ 添加模型' }}
          </button>
        </div>

        <div v-if="adding" class="add-box">
          <div class="add-grid">
            <label>模型 ID
              <input
                v-model="draft.id"
                list="preset-models"
                placeholder="从接口返回的模型里选，或直接输入 model-id"
              />
              <datalist id="preset-models">
                <option v-for="item in remoteModels" :key="item" :value="item" />
              </datalist>
              <small v-if="remoteModels.length">
                接口返回 {{ remoteModels.length }} 个模型，下拉可直接选择
              </small>
            </label>
            <label>展示名<input v-model="draft.name" /></label>
            <label>上下文<input v-model="draft.context" /></label>
            <label>输出<input v-model="draft.output" /></label>
          </div>
          <div class="remote-models">
            <div class="remote-head">
              <span>接口可用模型</span>
              <button type="button" class="mini" :disabled="remoteLoading" @click="loadRemoteModels">
                {{ remoteLoading ? '读取中…' : '刷新列表' }}
              </button>
            </div>
            <span v-if="remoteError" class="remote-error">{{ remoteError }}</span>
            <div v-else-if="remoteModels.length" class="remote-list">
              <button
                v-for="item in remoteModels"
                :key="item"
                type="button"
                class="remote-chip"
                @click="pickRemoteModel(item)"
              >
                {{ item }}
              </button>
            </div>
            <span v-else class="remote-hint">
              点「刷新列表」从接口读取可用模型；接口不支持列表时可直接手动填写。
            </span>
          </div>
          <div class="chips">
            <span>上下文</span>
            <button v-for="value in ['256K', '512K', '1M']" :key="value" type="button" @click="draft.context = value">{{ value }}</button>
            <span>输出</span>
            <button v-for="value in ['32K', '64K', '128K']" :key="value" type="button" @click="draft.output = value">{{ value }}</button>
          </div>
          <div class="input-types">
            <span>输入类型</span>
            <em class="on">✓ 文本</em><em class="on">✓ 图像</em>
            <em>音频</em><em>视频</em><em>PDF</em>
          </div>
          <div class="add-actions">
            <button type="button" class="mini" @click="adding = false">取消</button>
            <button type="button" class="primary small" @click="addModel">保存模型</button>
          </div>
        </div>

        <div class="model-list">
          <button
            v-for="item in form.models"
            :key="item.id"
            type="button"
            :class="['model-row', { active: item.id === form.model }]"
            :disabled="busy"
            @click="applyModel(item)"
          >
            <span class="radio"></span>
            <span class="model-main">
              <strong>{{ item.id }}</strong>
              <small>{{ item.name }}</small>
            </span>
            <span class="tags">
              <em>{{ item.context }}</em><em>输出 {{ item.output }}</em><em>全模态</em>
            </span>
            <span v-if="item.id === form.model" class="current">当前</span>
            <span
              v-else
              class="remove"
              role="button"
              title="删除"
              @click.stop="removeModel(item.id)"
            >×</span>
          </button>
        </div>

        <label class="budget">单次输出上限（tokens）
          <input v-model.number="form.output_limit" type="number" min="1000" max="200000" step="1000" />
          <small>推理型模型会先消耗思考 token，上限过小会导致正文被截断。</small>
        </label>
      </div>

      <footer class="panel-foot">
        <button type="button" class="mini" @click="$emit('close')">取消</button>
        <button type="button" class="primary" :disabled="busy || !config" @click="save()">
          {{ busy ? '处理中…' : '保存并应用' }}
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.mask { position: fixed; inset: 0; z-index: 80; display: flex; align-items: center; justify-content: center; background: rgba(15, 23, 42, .45); padding: 20px; }
.panel { width: min(720px, 100%); max-height: 88vh; overflow: auto; background: #fff; border-radius: 14px; box-shadow: 0 24px 70px rgba(15,23,42,.22); }
.panel-head { display: flex; justify-content: space-between; gap: 16px; padding: 20px 24px 14px; border-bottom: 1px solid #e8edf2; }
.panel-head h2 { margin: 0 0 4px; font-size: 18px; color: #1e293b; }
.panel-head p { margin: 0; font-size: 12px; color: #64748b; }
.close { border: 0; background: transparent; font-size: 24px; line-height: 1; color: #94a3b8; cursor: pointer; }
.body { padding: 18px 24px; }
.alert { margin: 0 24px 12px; padding: 8px 12px; border-radius: 8px; font-size: 12px; }
.alert.error { background: #fef2f2; color: #b42318; }
.alert.ok { background: #ecfdf5; color: #047857; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 16px; }
.grid label { display: grid; gap: 6px; font-size: 12px; font-weight: 650; color: #475569; }
.grid label.wide { grid-column: 1 / -1; }
input, select { width: 100%; height: 36px; padding: 0 10px; border: 1px solid #dbe3ec; border-radius: 8px; background: #fff; color: #1e293b; font-size: 13px; box-sizing: border-box; }
select:disabled { background: #f8fafc; color: #64748b; }
small { font-weight: 400; font-size: 11px; color: #94a3b8; }
.key-row { display: flex; gap: 8px; }
.key-row input { flex: 1; }
.test-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 16px 0; padding: 10px 12px; border: 1px solid #e8edf2; border-radius: 9px; background: #fafbfc; }
.test-state { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #64748b; }
.test-state i { width: 8px; height: 8px; border-radius: 50%; background: #cbd5e1; }
.test-state.ok i { background: #22c55e; }
.test-state.ok { color: #047857; }
.section-head { display: flex; align-items: center; gap: 10px; margin: 18px 0 10px; }
.section-head strong { font-size: 13px; color: #1e293b; }
.section-head span { flex: 1; font-size: 11px; color: #94a3b8; }
.add-box { padding: 14px; border: 1px solid #e2e8f0; border-radius: 10px; background: #fafbfc; margin-bottom: 12px; }
.add-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; }
.add-grid label { display: grid; gap: 5px; font-size: 11px; font-weight: 650; color: #475569; }
.chips, .input-types { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 10px; font-size: 11px; color: #94a3b8; }
.chips button { padding: 3px 10px; border: 1px solid #dbe3ec; border-radius: 6px; background: #fff; color: #475569; cursor: pointer; }
.input-types em { padding: 3px 9px; border: 1px solid #e2e8f0; border-radius: 6px; font-style: normal; background: #fff; }
.input-types em.on { border-color: #0f8f83; color: #0f8f83; background: #f0fdfa; }
.add-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.remote-models { margin-top: 12px; padding-top: 10px; border-top: 1px dashed #e2e8f0; }
.remote-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; font-size: 11px; color: #64748b; }
.remote-head span { flex: 1; font-weight: 650; }
.remote-list { display: flex; flex-wrap: wrap; gap: 6px; }
.remote-chip { padding: 4px 10px; border: 1px solid #dbe3ec; border-radius: 999px; background: #fff; color: #334155; font-size: 11px; cursor: pointer; }
.remote-chip:hover { border-color: #0f8f83; color: #0f8f83; background: #f0fdfa; }
.remote-error { font-size: 11px; color: #b42318; }
.remote-hint { font-size: 11px; color: #94a3b8; }
.model-list { display: grid; gap: 8px; }
.provider-list { display: grid; gap: 8px; margin-bottom: 14px; }
.provider-row { display: flex; align-items: center; gap: 12px; padding: 10px 12px; border: 1px solid #e8edf2; border-radius: 9px; background: #fff; }
.provider-row.active { border-color: #0f8f83; background: #f0fdfa; }
.provider-main { display: grid; gap: 2px; flex: 1; min-width: 200px; }
.provider-main strong { font-size: 13px; color: #1e293b; }
.provider-main small { font-size: 11px; color: #94a3b8; }
.proto { padding: 2px 8px; border-radius: 5px; background: #f1f5f9; color: #64748b; font-size: 10px; }
.provider-actions { display: flex; gap: 6px; }
.mini.danger { color: #b42318; border-color: #f3c7c3; }
.empty { margin: 0; font-size: 12px; color: #94a3b8; }
.model-row { display: flex; align-items: center; gap: 12px; width: 100%; padding: 10px 12px; border: 1px solid #e8edf2; border-radius: 9px; background: #fff; text-align: left; cursor: pointer; }
.model-row:hover { border-color: #0f8f83; }
.model-row.active { border-color: #0f8f83; background: #f0fdfa; }
.model-row:disabled { opacity: .6; cursor: default; }
.radio { width: 14px; height: 14px; flex: 0 0 14px; border: 2px solid #cbd5e1; border-radius: 50%; }
.model-row.active .radio { border-color: #0f8f83; background: radial-gradient(circle, #0f8f83 0 3px, transparent 4px); }
.model-main { display: grid; gap: 2px; min-width: 180px; flex: 1; }
.model-main strong { font-size: 13px; color: #1e293b; }
.model-main small { font-size: 11px; color: #94a3b8; }
.tags { display: flex; gap: 6px; flex-wrap: wrap; }
.tags em { padding: 2px 7px; border-radius: 5px; background: #f1f5f9; color: #64748b; font-size: 10px; font-style: normal; }
.current { padding: 2px 8px; border-radius: 5px; background: #0f8f83; color: #fff; font-size: 10px; }
.remove { color: #94a3b8; font-size: 16px; padding: 0 4px; }
.budget { display: grid; gap: 6px; max-width: 260px; margin-top: 16px; font-size: 12px; font-weight: 650; color: #475569; }
.panel-foot { display: flex; justify-content: flex-end; gap: 10px; padding: 14px 24px 20px; border-top: 1px solid #e8edf2; }
.mini { padding: 6px 12px; border: 1px solid #dbe3ec; border-radius: 7px; background: #fff; color: #475569; font-size: 12px; cursor: pointer; }
.mini:disabled { opacity: .55; cursor: default; }
.primary { padding: 8px 16px; border: 0; border-radius: 8px; background: #0f8f83; color: #fff; font-size: 13px; font-weight: 650; cursor: pointer; }
.primary.small { padding: 6px 14px; font-size: 12px; }
.primary:disabled { opacity: .55; cursor: default; }
@media (max-width: 620px) { .grid, .add-grid { grid-template-columns: 1fr; } .tags { display: none; } }
</style>

<script setup lang="ts">
import { usePanelSync } from '../panelSync';
import { onBeforeUnmount, ref, watch } from 'vue';
import { request, type InputRun, type UnderstandingResult } from '../api/images';

type Product = UnderstandingResult['product'];
interface Confirmation {
  version: number;
  created_at: string;
  data: { expected_version: number; product: Product; reviewer: string; reason: string };
}
const props = defineProps<{
  run: InputRun;
  syncRevision?: number;
  product: Product;
  locked: boolean;
}>();
const emit = defineEmits<{ saved: []; confirmed: [value: boolean] }>();
// 人工确认是独立版本：字段来自识别结果，但后续业务应优先读取已保存的人工确认资料。
const version = ref(0);
const name = ref(props.product.product_name);
const type = ref(props.product.product_type);
const features = ref(props.product.features.join('\n'));
const uncertainties = ref(props.product.uncertainties.join('\n'));
const reviewer = ref('');
const reason = ref('');
const busy = ref(false);
const dirty = ref(false);
const saved = ref(false);
const history = ref<Confirmation[]>([]);
const error = ref('');
let epoch = 0;
let alive = true;
onBeforeUnmount(() => {
  alive = false;
  epoch++;
});
const base = () =>
  `/api/v1/projects/${props.run.project_id}/pipeline-runs/${props.run.id}/product-confirmation`;
const object = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
function valid(v: unknown): v is Confirmation {
  if (!object(v) || !object(v.data) || !object(v.data.product)) return false;
  const p = v.data.product;
  return (
    typeof v.version === 'number' &&
    Number.isSafeInteger(v.version) &&
    v.version > 0 &&
    typeof v.created_at === 'string' &&
    typeof v.data.reviewer === 'string' &&
    typeof v.data.reason === 'string' &&
    typeof p.product_name === 'string' &&
    typeof p.product_type === 'string' &&
    [p.features, p.uncertainties].every(
      (a) => Array.isArray(a) && a.every((x) => typeof x === 'string'),
    )
  );
}
function restore(value: Confirmation) {
  const p = value.data.product;
  version.value = value.version;
  name.value = p.product_name;
  type.value = p.product_type;
  features.value = p.features.join('\n');
  uncertainties.value = p.uncertainties.join('\n');
  reviewer.value = value.data.reviewer;
  reason.value = value.data.reason;
  dirty.value = false;
  saved.value = true;
  emit('confirmed', true);
}
function change() {
  dirty.value = true;
  saved.value = false;
  emit('confirmed', false);
}
async function load() {
  const token = ++epoch;
  busy.value = true;
  error.value = '';
  try {
    const value = await request<unknown>(base() + '/history');
    if (!alive || token !== epoch) return;
    if (!Array.isArray(value) || !value.every(valid)) throw new Error('商品确认记录格式不正确');
    history.value = value;
    if (value[0]) restore(value[0]);
    else emit('confirmed', props.locked);
  } catch (e) {
    if (alive && token === epoch) error.value = e instanceof Error ? e.message : '读取失败';
  } finally {
    if (alive && token === epoch) busy.value = false;
  }
}
const lines = (v: string) =>
  v
    .split('\n')
    .map((x) => x.trim())
    .filter(Boolean);
async function save() {
  const token = ++epoch;
  busy.value = true;
  error.value = '';
  try {
    const value = await request<unknown>(base(), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_version: version.value,
        product: {
          product_name: name.value,
          product_type: type.value,
          features: lines(features.value),
          uncertainties: lines(uncertainties.value),
        },
        reviewer: reviewer.value,
        reason: reason.value,
      }),
    });
    if (!alive || token !== epoch) return;
    if (!valid(value)) throw new Error('商品确认响应格式不正确');
    history.value = [value, ...history.value];
    restore(value);
    emit('saved');
  } catch (e) {
    if (alive && token === epoch) error.value = e instanceof Error ? e.message : '保存失败';
  } finally {
    if (alive && token === epoch) busy.value = false;
  }
}
const syncPending = usePanelSync(
  () => props.syncRevision,
  () => busy.value || dirty.value,
  load,
);
void load();
watch(
  () => props.locked,
  (value) => {
    if (value && !history.value.length && !dirty.value) emit('confirmed', true);
  },
);
</script>

<template>
  <section class="product-confirmation" aria-label="人工确认商品资料">
    <p v-if="syncPending" role="status" class="sync-notice">
      后台结果有更新；当前操作或未保存修改结束后将自动同步。也可使用面板的读取按钮重新加载。
    </p>
    <div class="confirmation-heading">
      <strong>{{ locked ? '商品资料' : '2. 核对商品资料' }}</strong>
      <span v-if="locked" class="confirmation-badge">{{
        saved ? `已确认 · v${version}` : '已锁定'
      }}</span>
      <button v-else type="button" :disabled="busy" @click="load">重新读取（丢弃修改）</button>
    </div>
    <p v-if="!locked">核对名称、类型和特征，保存后系统将提取商品四维基准。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <!-- 锁定状态以只读文字展示，避免把无法编辑的灰色表单误当作待办。 -->
    <div v-if="locked" class="product-summary">
      <h3>{{ name }}</h3>
      <span class="product-type">{{ type }}</span>
      <h4>核心特征</h4>
      <ul v-if="lines(features).length">
        <li v-for="(feature, index) in lines(features)" :key="index">{{ feature }}</li>
      </ul>
      <p v-else>暂无已记录特征。</p>
      <details v-if="lines(uncertainties).length" class="uncertainty-details">
        <summary>待确认信息 · {{ lines(uncertainties).length }} 项</summary>
        <ul>
          <li v-for="(item, index) in lines(uncertainties)" :key="index">{{ item }}</li>
        </ul>
      </details>
      <p class="locked-note">资料已用于商品词分析。需要修改商品身份时，请新建研究。</p>
    </div>
    <form v-else @submit.prevent="save">
      <fieldset :disabled="busy || locked" @input="change" @change="change">
        <legend>商品资料</legend>
        <div class="confirmation-fields">
          <label
            >商品名称<input v-model="name" aria-label="确认商品名称" required maxlength="500"
          /></label>
          <label
            >商品类型<input v-model="type" aria-label="确认商品类型" required maxlength="500"
          /></label>
          <label
            >核心特征（每行一项）<textarea v-model="features" aria-label="确认核心特征" />
          </label>
          <label
            >待确认信息（每行一项）<textarea v-model="uncertainties" aria-label="确认待补信息" />
          </label>
          <label
            >确认人（本地记录）<input
              v-model="reviewer"
              aria-label="商品确认人"
              required
              maxlength="200"
          /></label>
          <label
            >确认或修改说明<input
              v-model="reason"
              aria-label="商品确认说明"
              required
              maxlength="200"
          /></label>
        </div>
        <button type="submit" :disabled="saved && !dirty">确认商品资料</button>
      </fieldset>
    </form>
    <p v-if="saved && !locked">商品资料已确认 · v{{ version }}</p>
    <p v-else-if="!locked">请先确认商品资料，再继续确认四维基准。</p>
    <details v-if="history.length">
      <summary>确认记录（{{ history.length }} 次）</summary>
      <p v-for="h in history" :key="h.version">
        v{{ h.version }} · {{ h.data.reviewer }} · {{ h.data.reason }} ·
        {{ h.data.product.product_type }}
      </p>
    </details>
  </section>
</template>

<style scoped>
.product-confirmation {
  margin-top: 16px;
  padding: 16px;
  border: 1px solid #dde7e1;
  border-radius: 10px;
  background: #fff;
}
.confirmation-heading {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
}
.confirmation-badge {
  font-size: 11px;
  font-weight: 500;
  color: #236a53;
  background: #edf6f1;
  border-radius: 20px;
  padding: 4px 9px;
  white-space: nowrap;
}
.product-summary h3 {
  font-size: 17px;
  line-height: 1.6;
  margin: 16px 0 8px;
  color: #243e34;
  overflow-wrap: anywhere;
}
.product-type {
  display: inline-block;
  padding: 3px 9px;
  font-size: 12px;
  border-radius: 5px;
  background: #f1f4f2;
  color: #566b60;
}
.product-summary h4 {
  font-size: 12px;
  color: #6c7c73;
  margin: 20px 0 8px;
  font-weight: 500;
}
.product-summary ul {
  margin: 8px 0 16px;
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.85;
  color: #40574b;
}
.product-summary li + li {
  margin-top: 5px;
}
.product-confirmation details {
  border-top: 1px solid #e8ede9;
  padding: 12px 0 0;
  margin-top: 12px;
  color: #63766a;
}
.product-confirmation summary {
  cursor: pointer;
}
.product-summary .locked-note {
  margin: 16px 0 4px;
  color: #8a958e;
}
.product-confirmation p {
  font-size: 12px;
  line-height: 1.7;
  color: #75887b;
}
.product-confirmation p[role='alert'] {
  color: #b42318;
}
.confirmation-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.confirmation-fields label {
  display: grid;
  gap: 6px;
  font-size: 12px;
}
.confirmation-fields input,
.confirmation-fields textarea {
  border: 1px solid #d5e1d9;
  border-radius: 6px;
  padding: 9px;
  font: inherit;
  width: 100%;
  min-width: 0;
}
.confirmation-fields textarea {
  min-height: 76px;
  resize: vertical;
}
.product-confirmation fieldset {
  border: 0;
  padding: 0;
  margin: 12px 0;
  min-width: 0;
}
.product-confirmation legend {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
}
.product-confirmation button {
  border: 1px solid #cadbd0;
  border-radius: 6px;
  background: #f6faf7;
  color: #28624a;
  padding: 7px 10px;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.product-confirmation button:disabled {
  opacity: 0.5;
  cursor: default;
}
.product-confirmation details {
  font-size: 12px;
}
@media (max-width: 600px) {
  .confirmation-fields {
    grid-template-columns: minmax(0, 1fr);
  }
  .confirmation-heading {
    align-items: start;
    flex-direction: column;
  }
}
</style>

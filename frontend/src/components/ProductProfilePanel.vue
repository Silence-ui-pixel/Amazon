<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue';
import type { InputRun } from '../api/images';
import {
  confirmProductProfile,
  generateProductProfile,
  readProductProfile,
  type ProductProfileRecord,
} from '../api/productProfiles';

const props = defineProps<{ run: InputRun; enabled: boolean; locked: boolean }>();
const emit = defineEmits<{ confirmed: [value: boolean]; saved: [] }>();
const record = ref<ProductProfileRecord | null>(null);
const purpose = ref('');
const style = ref('');
const specifications = ref('');
const quantity = ref('1');
const unit = ref('each');
const referencePrice = ref('');
const busy = ref(false);
const error = ref('');
let alive = true;
onBeforeUnmount(() => (alive = false));
const lines = (value: string) =>
  value
    .split('\n')
    .map((x) => x.trim())
    .filter(Boolean);
function restore(value: ProductProfileRecord) {
  record.value = value;
  purpose.value = value.profile.purpose.join('\n');
  style.value = value.profile.style_structure.join('\n');
  specifications.value = value.profile.specifications.join('\n');
  quantity.value = value.profile.package_quantity ?? '1';
  unit.value = value.profile.unit_basis ?? 'each';
  emit('confirmed', value.status === 'confirmed');
}
async function load(generate = false) {
  busy.value = true;
  error.value = '';
  try {
    const value =
      (await readProductProfile(props.run)) ??
      (generate ? await generateProductProfile(props.run) : null);
    if (!alive) return;
    if (value) restore(value);
  } catch (reason) {
    if (alive) error.value = reason instanceof Error ? reason.message : '四维基准读取失败';
  } finally {
    if (alive) busy.value = false;
  }
}
async function save() {
  if (!record.value) return;
  busy.value = true;
  error.value = '';
  try {
    const value = await confirmProductProfile(props.run, {
      expected_version: record.value.version,
      purpose: lines(purpose.value),
      style_structure: lines(style.value),
      specifications: lines(specifications.value),
      package_quantity: quantity.value,
      unit_basis: unit.value.trim(),
      currency: record.value.profile.currency ?? 'USD',
      ...(record.value.profile.price_source === 'user_reference' && record.value.profile.price
        ? { reference_price: record.value.profile.price }
        : {}),
    });
    if (!alive) return;
    restore(value);
    emit('saved');
  } catch (reason) {
    if (alive) error.value = reason instanceof Error ? reason.message : '四维基准保存失败';
  } finally {
    if (alive) busy.value = false;
  }
}
async function generateWithReference() {
  const value = referencePrice.value.trim();
  if (!/^\d+(\.\d{1,4})?$/.test(value) || Number(value) <= 0) {
    error.value = '请输入大于 0 的当前整套售价（USD）';
    return;
  }
  busy.value = true;
  error.value = '';
  try {
    const generated = await generateProductProfile(props.run, value);
    if (!alive) return;
    restore(generated);
    emit('saved');
  } catch (reason) {
    if (alive) error.value = reason instanceof Error ? reason.message : '四维基准生成失败';
  } finally {
    if (alive) busy.value = false;
  }
}
watch(
  () => props.enabled,
  (enabled) => {
    if (enabled && !record.value) void load(true);
  },
  { immediate: true },
);
</script>

<template>
  <section class="product-profile" aria-label="商品四维基准">
    <div class="profile-heading">
      <strong>3. 确认商品四维基准</strong>
      <span v-if="record"
        >v{{ record.version }} · {{ record.status === 'confirmed' ? '已确认' : '待确认' }}</span
      >
    </div>
    <p>用途和款式结构各占 35%，规格占 20%，可靠价格占 10%。</p>
    <p v-if="!enabled">请先确认商品资料。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="busy">正在提取四维资料…</p>
    <form
      v-if="enabled && !record"
      class="reference-price-retry"
      @submit.prevent="generateWithReference"
    >
      <label>
        页面未读到可靠当前价时，填写本品整套售价（USD）
        <input
          v-model="referencePrice"
          type="number"
          min="0.0001"
          step="0.0001"
          inputmode="decimal"
          placeholder="例如：39.99"
          :disabled="busy"
          required
        />
      </label>
      <p>ASIN 会先尝试读取 Amazon 当前价；只有页面没有当前价、显示区间价或优惠价时才需要填写。</p>
      <button type="submit" :disabled="busy">使用该售价重试</button>
    </form>
    <form v-if="record" @submit.prevent="save">
      <fieldset :disabled="busy || locked">
        <label>用途（每行一项）<textarea v-model="purpose" required /></label>
        <label>款式与结构（每行一项）<textarea v-model="style" required /></label>
        <label>规格（每行一项）<textarea v-model="specifications" required /></label>
        <label
          >包装数量<input v-model="quantity" type="number" min="0.0001" step="any" required
        /></label>
        <label>计价单位<input v-model="unit" required maxlength="100" /></label>
        <div class="profile-price">
          可靠售价：{{ record.profile.currency }} {{ record.profile.price }} ·
          {{ record.profile.price_source === 'asin_current' ? 'ASIN 当前价' : '用户参考售价' }}
        </div>
        <button type="submit">
          {{ record.status === 'confirmed' ? '保存为新版本并重新分析' : '确认四维基准' }}
        </button>
      </fieldset>
    </form>
  </section>
</template>

<style scoped>
.product-profile {
  margin-top: 16px;
  padding: 16px;
  border: 1px solid #dde7e1;
  border-radius: 10px;
  background: #fff;
}
.profile-heading {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.product-profile p,
.profile-heading span {
  font-size: 12px;
  color: #75887b;
}
.product-profile p[role='alert'] {
  color: #b42318;
}
fieldset {
  border: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
label {
  display: grid;
  gap: 6px;
  font-size: 12px;
}
textarea,
input {
  border: 1px solid #d5e1d9;
  border-radius: 6px;
  padding: 9px;
  font: inherit;
}
textarea {
  min-height: 70px;
}
.profile-price {
  grid-column: 1 / -1;
  font-size: 13px;
  color: #40574b;
}
.reference-price-retry {
  display: grid;
  gap: 10px;
}
.reference-price-retry p {
  margin: 0;
}
button {
  width: max-content;
  padding: 8px 12px;
  border: 1px solid #cadbd0;
  border-radius: 6px;
  background: #f6faf7;
  color: #28624a;
}
@media (max-width: 600px) {
  fieldset {
    grid-template-columns: 1fr;
  }
}
</style>

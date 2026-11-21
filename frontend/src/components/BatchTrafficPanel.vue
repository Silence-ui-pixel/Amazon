<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import {
  trafficApi,
  type TrafficReport,
  type TrafficRow,
  type TrafficReview,
} from '../api/traffic';
import type { InputRun } from '../api/images';
const props = defineProps<{
  run: InputRun;
  report: TrafficReport;
  rows: TrafficRow[];
  disabled: boolean;
  reset: number;
}>();
const emit = defineEmits<{ saved: [report: TrafficReport]; dirty: [value: boolean] }>();
const selected = ref<string[]>([]);
const version = ref(props.report.version);
const decision = ref<TrafficReview['decision']>('defer');
const priority = ref<TrafficReview['priority']>('normal');
const reason = ref('');
const reviewer = ref('');
const evidence = ref('');
const error = ref('');
const busy = ref(false);
const formDirty = ref(false);
const eligible = computed(() => props.rows.filter((r) => !r.cleaning_exclusion));
let epoch = 0;
let alive = true;
onBeforeUnmount(() => {
  alive = false;
  epoch++;
});
watch(selected, (ids, old) => {
  if (!old.length) version.value = props.report.version;
});
watch([selected, formDirty, busy], () => {
  emit('dirty', selected.value.length > 0 || formDirty.value || busy.value);
});
watch(
  () => props.reset,
  () => {
    selected.value = [];
    formDirty.value = false;
    reason.value = '';
    reviewer.value = '';
    evidence.value = '';
    error.value = '';
    epoch++;
    busy.value = false;
  },
);
async function save() {
  const token = ++epoch;
  busy.value = true;
  error.value = '';
  try {
    const value = await trafficApi.batch(props.run, props.report.id, {
      expected_version: version.value,
      keywords: selected.value,
      decision: decision.value,
      priority: priority.value,
      reason: reason.value,
      reviewer: reviewer.value,
      evidence_note: evidence.value,
    });
    if (!alive || token !== epoch) return;
    selected.value = [];
    reason.value = '';
    evidence.value = '';
    formDirty.value = false;
    emit('saved', value);
  } catch (e) {
    if (alive && token === epoch) error.value = e instanceof Error ? e.message : '批量保存失败';
  } finally {
    if (alive && token === epoch) busy.value = false;
  }
}
</script>
<template>
  <details class="batch-traffic">
    <summary>批量审核本页关键词</summary>
    <p>
      只修改勾选词的决定和优先级。原有款式、价格、同类占比证据逐词保留；未填写的相关性仍为未知。
    </p>
    <form @submit.prevent="save" @input="formDirty = true" @change="formDirty = true">
      <fieldset :disabled="disabled || busy || report.stale">
        <legend>批量人工决定</legend>
        <div class="batch-actions">
          <button type="button" @click="selected = eligible.map((r) => r.phrase.keyword)">
            选择本页可审核词</button
          ><button type="button" @click="selected = []">清空选择</button
          ><span>已选 {{ selected.length }} 词 · 提交基于审核 v{{ version }}</span>
        </div>
        <div class="batch-keywords">
          <label v-for="r in eligible" :key="r.phrase.keyword"
            ><input
              v-model="selected"
              :value="r.phrase.keyword"
              type="checkbox"
              :aria-label="`批量选择 ${r.phrase.keyword}`"
            />{{ r.phrase.keyword
            }}<small v-if="r.metrics.rank === null || r.review?.relevance !== 'high'"
              >需注意证据</small
            ></label
          >
        </div>
        <div class="batch-fields">
          <label
            >批量决定<select v-model="decision" aria-label="批量决定">
              <option value="defer">待补数据</option>
              <option value="keep">保留</option>
              <option value="exclude">排除</option>
            </select></label
          ><label
            >批量优先级<select v-model="priority" aria-label="批量优先级">
              <option value="normal">普通</option>
              <option value="high">高</option>
              <option value="low">低</option>
            </select></label
          ><label
            >批量审核人<input
              v-model="reviewer"
              aria-label="批量审核人"
              required
              maxlength="100" /></label
          ><label
            >批量审核理由<input
              v-model="reason"
              aria-label="批量审核理由"
              required
              maxlength="200" /></label
          ><label
            >本次人工核对说明<input
              v-model="evidence"
              aria-label="批量核对说明"
              required
              maxlength="200"
          /></label>
        </div>
        <p v-if="decision === 'exclude' && selected.length">
          将排除 {{ selected.length }} 个词，请逐词确认包含长尾词在内的选择范围。
        </p>
        <button type="submit" :disabled="!selected.length">
          保存 {{ selected.length }} 个词的批量审核
        </button>
      </fieldset>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
  </details>
</template>
<style scoped>
.batch-traffic {
  margin: 16px 0;
  padding: 14px;
  border: 1px solid #d6e4dc;
  border-radius: 9px;
  background: #f8fbf9;
  font-size: 12px;
}
.batch-traffic summary {
  cursor: pointer;
  color: #32634b;
}
.batch-traffic p {
  line-height: 1.8;
  color: #718478;
}
.batch-traffic p[role='alert'] {
  color: #b42318;
}
.batch-traffic fieldset {
  border: 0;
  padding: 0;
  min-width: 0;
}
.batch-traffic legend {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
}
.batch-actions {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  margin: 12px 0;
}
.batch-keywords {
  display: flex;
  gap: 8px 16px;
  flex-wrap: wrap;
  padding: 12px 0;
}
.batch-keywords label {
  display: flex;
  align-items: center;
  gap: 6px;
  overflow-wrap: anywhere;
}
.batch-keywords small {
  color: #a07538;
}
.batch-fields {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 12px 0;
}
.batch-fields label {
  display: grid;
  gap: 6px;
}
.batch-fields input,
.batch-fields select {
  border: 1px solid #ceded2;
  border-radius: 5px;
  padding: 8px;
  font: inherit;
  width: 100%;
  min-width: 0;
}
.batch-traffic button {
  padding: 7px 10px;
  border: 1px solid #c8dacc;
  border-radius: 6px;
  background: #fff;
  color: #286349;
  cursor: pointer;
}
.batch-traffic button:disabled {
  opacity: 0.5;
  cursor: default;
}
@media (max-width: 600px) {
  .batch-fields {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>

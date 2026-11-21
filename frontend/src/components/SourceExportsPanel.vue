<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, watch, type ComputedRef } from 'vue';
import { request, type InputRun } from '../api/images';

type ExportRecord = {
  id: string; kind: string; owner: string; query: string; status: string;
  error_code: string; error: string; filename: string; sha256: string; download_name: string;
  source_type?: string; source_total?: number; source_rows?: number; truncated?: boolean; stop_reason?: string;
};
const props = defineProps<{ run: InputRun; kinds?: string[] }>();
const records = ref<ExportRecord[]>([]);
const sourceSync = inject<ComputedRef<{ active: boolean; revision: number }>>('source-export-sync');
const error = ref('');
const busy = ref('');
let generation = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
const base = computed(() => `/api/v1/projects/${props.run.project_id}/pipeline-runs/${props.run.id}/source-exports`);
const visible = computed(() => records.value.filter(item => !props.kinds || props.kinds.includes(item.kind)));
const labels: Record<string, string> = {
  queued: '等待处理', downloading: '平台原件下载中', parsing: '正在计算指标', completed: '文件及计算完成',
  download_failed: '下载失败', parse_failed: '文件已保存，解析失败',
};
async function load(epoch: number) {
  try {
    const items = await request<ExportRecord[]>(base.value);
    if (epoch !== generation) return;
    records.value = items;
    error.value = '';
  } catch (err) {
    if (epoch === generation) error.value = err instanceof Error ? err.message : '读取下载状态失败';
  } finally {
    if (epoch === generation) {
      clearTimeout(timer);
      if (error.value || sourceSync?.value.active || records.value.some(item => ['queued', 'downloading', 'parsing'].includes(item.status)))
        timer = setTimeout(() => void load(epoch), 5000);
    }
  }
}
async function retry(item: ExportRecord) {
  const epoch = generation;
  busy.value = item.id;
  try {
    const updated = await request<ExportRecord>(`${base.value}/${item.id}/retry`, { method: 'POST' });
    if (epoch !== generation) return;
    records.value = records.value.map(row => row.id === item.id ? updated : row);
    error.value = '';
  } catch (err) {
    if (epoch === generation) error.value = err instanceof Error ? err.message : '重试失败';
  } finally {
    if (epoch === generation) { busy.value = ''; void load(epoch); }
  }
}
watch(() => sourceSync?.value, () => { clearTimeout(timer); void load(generation); });
watch(() => [props.run.project_id, props.run.id], () => {
  generation++;
  clearTimeout(timer);
  records.value = [];
  error.value = '';
  busy.value = '';
  void load(generation);
}, { immediate: true });
onBeforeUnmount(() => { generation++; clearTimeout(timer); });
</script>

<template>
  <details class="source-exports" :open="visible.some(item => item.status.endsWith('failed'))">
    <summary>采集表与计算结果 <span>{{ visible.length }} 份</span></summary>
    <p class="help">接口采集表与历史平台原件分别标注；计算表在搜索词后加入购买能力、垄断程度。</p>
    <p v-if="!kinds" class="help">这里显示当前研究的下载状态。第八步附表保存生成时的来源快照；之后补齐的下载可用“更新草稿附表”纳入，已发布版本保持原样。</p>
    <p v-if="error" class="failure" role="alert">{{ error }}</p>
    <p v-if="!visible.length" class="help">暂无采集表。</p>
    <article v-for="item in visible" :key="item.id">
      <div class="export-title"><strong>{{ item.download_name }}</strong><span>{{ item.status === 'completed' && item.source_type === 'api_generated' ? '接口表已生成（非平台原件）' : (labels[item.status] || item.status) }}</span></div>
      <p v-if="item.error" class="failure">{{ item.error }} <small>{{ item.error_code }}</small></p>
      <p v-if="item.source_type === 'api_generated' && item.source_rows !== undefined" :class="item.truncated ? 'failure' : 'help'">接口采集 {{ item.source_rows }} / {{ item.source_total }} 条{{ item.truncated ? '，结果未完整获取' : item.stop_reason === 'rank_limit' ? '，已到排名边界' : '' }}</p>
      <p v-else-if="item.status === 'queued'" class="help">等待本研究绑定的采集器执行，请保持采集器与紫鸟在线。</p>
      <p v-else-if="item.status === 'downloading'" class="help">正在从平台下载原件，完成后自动计算指标。</p>
      <p v-else-if="item.status === 'parsing'" class="help">文件已保存，正在读取搜索词与前三商品份额。</p>
      <div class="export-actions">
        <a v-if="item.sha256" :href="`${base}/${item.id}/file?original=true`" download>{{ item.source_type === 'api_generated' ? '下载接口采集表' : '下载平台原件' }}</a>
        <a v-if="item.status === 'completed'" :href="`${base}/${item.id}/file`" download>下载计算表</a>
      <button v-if="item.status.endsWith('failed') || (item.status === 'completed' && item.source_type === 'api_generated')" type="button" :disabled="busy === item.id" @click="retry(item)">{{ busy === item.id ? '正在提交…' : item.source_type === 'api_generated' ? '重新下载平台原件' : '重试下载' }}</button>
      </div>
    </article>
  </details>
</template>

<style scoped>
.source-exports { margin: 18px 0; border: 1px solid #dce4ec; border-radius: 10px; padding: 14px 16px; background: #f8fafc; }
summary { cursor: pointer; font-weight: 600; } summary span { margin-left: 8px; font-weight: 400; color: #64748b; }
article { padding: 12px 0; border-top: 1px solid #e2e8f0; } .export-title { display: flex; gap: 12px; justify-content: space-between; flex-wrap: wrap; }
.export-title strong { overflow-wrap: anywhere; } .export-title span, .help { color: #64748b; font-size: 13px; } .help { line-height: 1.7; }
.failure { color: #b42318; overflow-wrap: anywhere; font-size: 13px; } .export-actions { display: flex; gap: 16px; margin-top: 10px; align-items: center; }
a { color: #1d4ed8; font-size: 13px; } button { cursor: pointer; padding: 5px 10px; border: 1px solid #cbd5e1; border-radius: 6px; background: white; }
</style>

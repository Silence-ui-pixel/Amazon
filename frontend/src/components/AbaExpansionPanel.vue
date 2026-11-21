<script setup lang="ts">
import SourceExportsPanel from './SourceExportsPanel.vue';
import { usePanelSync } from '../panelSync';
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import {
  expansionApi,
  queryDownloadable,
  type Direction,
  type ExpansionPage,
  type ExpansionReport,
  type ExpansionWorkspace,
} from '../api/abaExpansion';
import type { InputRun } from '../api/images';

const props = defineProps<{
  run: InputRun;
  syncRevision?: number;
  coreReady: boolean;
  competitorsReady: boolean;
  coreSelectionId: string;
  competitorSelectionId: string;
  marketplace: string;
}>();
const emit = defineEmits<{ status: [value: 'waiting' | 'running' | 'review' | 'done'] }>();
const week = defineModel<string>('reportWeek', { required: true });
// 这是每次查询的采集上限；正反向进度、截断与失效以服务端报告为准。
const maxRows = ref(1000);
const data = ref<ExpansionWorkspace | null>(null);
const selectedId = ref('');
const rows = ref<ExpansionPage | null>(null);
const offset = ref(0);
const busy = ref(false);
// Read-only restoration must not change saved execution status.
const reading = ref(true);
const readingRows = ref(false);
const error = ref('');
const rowError = ref('');
let epoch = 0;
let rowEpoch = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let alive = true;
onBeforeUnmount(() => {
  alive = false;
  epoch++;
  rowEpoch++;
  clearTimeout(timer);
});
const directions: Direction[] = ['forward', 'reverse'];
const name = (direction: Direction) => (direction === 'forward' ? '正向' : '反向');
const statusName = (report: ExpansionReport) =>
  report.stale
    ? '上游已变更 · 历史证据'
    : {
        running: '采集中',
        succeeded: '采集完整',
        partial: '部分结果',
        failed: '采集失败',
        interrupted: '服务中断',
      }[report.status];
const selected = computed(() => data.value?.reports.find((r) => r.id === selectedId.value));
const running = computed(() => data.value?.reports.some((r) => r.status === 'running') ?? false);
function latest(direction: Direction) {
  return data.value?.reports.find(
    (r) => r.direction === direction && r.week_end === week.value && !r.stale,
  );
}
watch(
  [data, reading, busy, error, week, () => props.coreReady, () => props.competitorsReady],
  () => {
    if (reading.value) return;
    emit(
      'status',
      busy.value || running.value
        ? 'running'
        : data.value?.complete && props.coreReady && props.competitorsReady
          ? 'done'
          : data.value?.reports.length || error.value
            ? 'review'
            : 'waiting',
    );
  },
  { immediate: true },
);
async function refresh(current = epoch) {
  reading.value = true;
  clearTimeout(timer);
  try {
    const result = await expansionApi.get(props.run, week.value);
    if (!alive || current !== epoch) return;
    data.value = result;
    error.value = '';
    if (!result.reports.some((r) => r.id === selectedId.value))
      selectedId.value = result.reports[0]?.id ?? '';
    if (result.reports.some((r) => r.status === 'running'))
      timer = setTimeout(() => {
        void refresh(current);
      }, 1500);
  } catch (e) {
    if (alive && current === epoch)
      error.value = e instanceof Error ? e.message : '读取拓词状态失败';
  } finally {
    if (alive && current === epoch) reading.value = false;
  }
}
watch(
  () => [props.run.id, props.coreSelectionId, props.competitorSelectionId, week.value],
  () => {
    epoch++;
    rowEpoch++;
    clearTimeout(timer);
    busy.value = false;
    data.value = null;
    selectedId.value = '';
    rows.value = null;
    error.value = '';
    void refresh();
  },
  { immediate: true },
);
async function collect(direction: Direction) {
  if (busy.value) return;
  if (!Number.isInteger(maxRows.value) || maxRows.value < 1 || maxRows.value > 10000) {
    error.value = '每个查询采集上限须为 1 至 10000 的整数';
    return;
  }
  const current = epoch;
  busy.value = true;
  error.value = '';
  try {
    const report = await expansionApi.collect(props.run, direction, week.value, maxRows.value);
    if (!alive || current !== epoch) return;
    await refresh(current);
    if (alive && current === epoch) selectedId.value = report.id;
  } catch (e) {
    if (alive && current === epoch) error.value = e instanceof Error ? e.message : '采集启动失败';
  } finally {
    if (alive && current === epoch) busy.value = false;
  }
}
async function loadRows() {
  const current = ++rowEpoch;
  const id = selectedId.value;
  rows.value = null;
  rowError.value = '';
  readingRows.value = false;
  if (!id) return;
  readingRows.value = true;
  try {
    const result = await expansionApi.rows(props.run, id, offset.value);
    if (alive && current === rowEpoch) rows.value = result;
  } catch (e) {
    if (alive && current === rowEpoch)
      rowError.value = e instanceof Error ? e.message : '词表读取失败';
  } finally {
    if (alive && current === rowEpoch) readingRows.value = false;
  }
}
watch(selectedId, () => {
  offset.value = 0;
  void loadRows();
});
watch(offset, () => {
  void loadRows();
});
// 轮询完成或有新进度时，刷新当前快照的分页证据。
watch(data, () => {
  if (selectedId.value) void loadRows();
});
async function download() {
  const report = selected.value;
  if (!report) return;
  const current = epoch;
  try {
    await expansionApi.download(props.run, report);
  } catch (e) {
    if (alive && current === epoch) error.value = e instanceof Error ? e.message : '下载失败';
  }
}
async function downloadQueryXlsx(report: ExpansionReport, queryIndex: number, query: string) {
  const current = epoch;
  try {
    await expansionApi.downloadQueryXlsx(props.run, report, queryIndex, query);
  } catch (e) {
    if (alive && current === epoch) error.value = e instanceof Error ? e.message : 'Excel 下载失败';
  }
}
const percent = (value: number | null) => (value === null ? '—' : `${(value * 100).toFixed(2)}%`);
usePanelSync(
  () => props.syncRevision,
  () => busy.value,
  () => refresh(),
);
</script>

<template>
  <section class="expansion-panel" aria-labelledby="expansion-title">
    <header>
      <div>
        <small>第四步 · ABA 流量入口</small>
        <h2 id="expansion-title">ABA 正向 / 反向拓词</h2>
        <p>核心大词正向扩展，已保留竞对反向查找；两路分别保存周报与下载词表。</p>
      </div>
      <button type="button" :disabled="busy" @click="refresh()">读取历史状态</button>
    </header>
    <p v-if="marketplace !== 'US'" class="notice">当前 ABA 适配器仅支持美国站。</p>
    <div class="conditions">
      <label>周报结束日（周六）<input v-model="week" type="date" :disabled="busy" /></label>
      <label
        >每个查询采集上限<input
          v-model.number="maxRows"
          type="number"
          min="1"
          max="10000"
          :disabled="busy"
      /></label>
    </div>
    <p class="hint">
      正向使用第二步已保存的全部核心词；反向把第三步经过价格核对并保留的全部竞对 ASIN（最多 100
      个）合并为一次查询。相同条件的完整结果缓存 7 天；刷新仅恢复记录。
    </p>
    <div class="directions">
      <article v-for="direction in directions" :key="direction">
        <h3>{{ direction === 'forward' ? '02 · ABA 正向查找' : '03 · ABA 反向查找' }}</h3>
        <p>
          {{
            direction === 'forward'
              ? '逐个查询全部已确认核心大词，汇总延伸关键词'
              : '把全部已保留竞对 ASIN 一次提交，下载反向关键词数据'
          }}
        </p>
        <p v-if="direction === 'forward' ? !coreReady : !competitorsReady" class="notice">
          {{ direction === 'forward' ? '请先保存第二步核心大词' : '请先保存第三步竞对选择' }}
        </p>
        <template v-if="latest(direction)">
          <strong>{{ statusName(latest(direction)!) }}</strong>
          <p>
            {{ latest(direction)!.queries.length }} 个查询 ·
            {{ latest(direction)!.queries.reduce((n, q) => n + q.fetched, 0) }} 行来源记录
          </p>
          <button type="button" @click="selectedId = latest(direction)!.id">
            查看{{ name(direction) }}词表
          </button>
        </template>
        <button
          type="button"
          :disabled="
            busy ||
            marketplace !== 'US' ||
            !week ||
            !coreReady ||
            (direction === 'reverse' && !competitorsReady) ||
            latest(direction)?.status === 'running'
          "
          @click="collect(direction)"
        >
          开始{{ name(direction) }}采集 / 重试
        </button>
      </article>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="data?.complete && coreReady && competitorsReady" class="success">
      本周正反向均已完整采集，第四步已完成。
    </p>
    <p v-if="data" class="comparison">
      当前周已采集结果：交集 {{ data.comparison.both }} · 仅正向
      {{ data.comparison.forward_only }} · 仅反向
      {{ data.comparison.reverse_only }}。两路完整后再判断流量差异。
    </p>
    <details class="business-history">
      <summary>历史快照与版本选择</summary>
      <label v-if="data?.reports.length" class="history"
        >历史采集记录<select v-model="selectedId">
          <option v-for="report in data.reports" :key="report.id" :value="report.id">
            {{ report.week_end }} · {{ name(report.direction) }} · {{ statusName(report) }} ·
            {{ new Date(report.created_at).toLocaleString() }}
          </option>
        </select></label
      >
    </details>
    <template v-if="selected">
      <div class="report-heading">
        <strong
          >{{ name(selected.direction) }}词表 · {{ selected.week_start }} —
          {{ selected.week_end }} · {{ statusName(selected) }}</strong
        ><button type="button" :disabled="selected.status === 'running'" @click="download">
          下载{{ name(selected.direction) }} CSV
        </button>
      </div>
      <p v-for="warning in selected.warnings" :key="warning" class="notice">{{ warning }}</p>
      <details>
        <summary>查询条件与采集进度（{{ selected.queries.length }} 项）</summary>
        <ul>
          <li v-for="(q, queryIndex) in selected.queries" :key="q.query">
            <strong>{{ q.query }}</strong> · {{ q.fetched }} / {{ q.total }} 行 · {{ q.pages }} 页
            <span v-if="q.error" class="error">{{ q.error }}</span
            ><span v-else-if="q.truncated" class="notice">结果截断：{{ q.stop_reason }}</span
            ><span v-else>{{ q.stop_reason || '等待处理' }}</span>
            <button
              type="button"
              :disabled="!queryDownloadable(selected, q)"
              @click="downloadQueryXlsx(selected, queryIndex, q.query)"
            >
              下载{{ name(selected.direction) }} Excel
            </button>
          </li>
        </ul>
      </details>
      <p v-if="readingRows">正在读取词表…</p>
      <p v-if="rowError" role="alert" class="error">{{ rowError }}</p>
      <div v-if="rows" class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>关键词</th>
              <th>周搜索排名</th>
              <th>来源 / 头部商品</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in rows.items" :key="item.keyword">
              <td>
                <strong>{{ item.keyword }}</strong
                ><small v-if="item.also_in_other_direction">两路均出现</small>
              </td>
              <td>
                {{ item.search_frequency_rank?.toLocaleString() ?? '排名冲突'
                }}<small v-if="item.rank_conflict" class="error">保留各来源排名，请核对</small>
              </td>
              <td>
                <details>
                  <summary>{{ item.evidence.length }} 条查询证据</summary>
                  <div v-for="(e, index) in item.evidence" :key="index" class="evidence">
                    <strong>{{ e.query }}</strong>
                    <p>原词：{{ e.keyword }} · 排名 {{ e.search_frequency_rank }}</p>
                    <p v-for="(product, p) in e.products" :key="p">
                      {{ product.asin }} · 点击 {{ percent(product.click_share) }} · 转化
                      {{ percent(product.conversion_share) }}
                    </p>
                  </div>
                </details>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="!rows.items.length">此快照暂未采集到关键词。</p>
      </div>
      <footer v-if="rows">
        <span
          >共 {{ rows.total }} 个去重关键词 · {{ offset + (rows.total ? 1 : 0) }}–{{
            Math.min(offset + 20, rows.total)
          }}</span
        ><button
          type="button"
          :disabled="!offset || readingRows"
          @click="offset = Math.max(0, offset - 20)"
        >
          上一页</button
        ><button
          type="button"
          :disabled="offset + 20 >= rows.total || readingRows"
          @click="offset += 20"
        >
          下一页
        </button>
      </footer>
    </template>
    <SourceExportsPanel :run="run" :kinds="['aba_forward', 'aba_reverse']" />
  </section>
</template>

<style scoped>
.expansion-panel {
  background: #fff;
  border: 1px solid #e0e9e3;
  border-radius: 14px;
  padding: 24px;
  margin-bottom: 24px;
  min-width: 0;
  color: #243e30;
}
header,
.conditions,
.report-heading,
footer {
  display: flex;
  gap: 14px;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
}
h2 {
  font-size: 20px;
  margin: 8px 0;
}
h3 {
  font-size: 15px;
}
p {
  font-size: 13px;
  line-height: 1.7;
}
small {
  display: block;
  color: #718376;
  font-size: 12px;
}
label {
  display: grid;
  gap: 7px;
  font-size: 13px;
  min-width: 0;
}
input,
select {
  font: inherit;
  border: 1px solid #d3dfd6;
  border-radius: 7px;
  padding: 9px;
  max-width: 100%;
  min-width: 0;
  background: #fff;
}
.conditions {
  justify-content: flex-start;
  margin: 20px 0 10px;
}
.directions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin: 20px 0;
}
.directions article {
  background: #f7faf8;
  border: 1px solid #e0e9e3;
  border-radius: 10px;
  padding: 16px;
  min-width: 0;
}
.directions button {
  margin: 6px 8px 0 0;
}
button {
  font: inherit;
  font-size: 12px;
  border: 1px solid #b9d1c1;
  background: #fff;
  color: #28603e;
  border-radius: 7px;
  padding: 9px 12px;
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
.notice {
  padding: 8px;
  background: #fff6dd;
  border-radius: 7px;
}
.hint,
.comparison {
  color: #718376;
}
.error {
  color: #b42e2e;
}
.success {
  color: #267342;
  background: #eef8f0;
  padding: 12px;
  border-radius: 8px;
}
.history {
  margin: 20px 0;
}
.report-heading {
  margin: 20px 0;
  font-size: 14px;
}
summary {
  cursor: pointer;
  font-size: 13px;
}
li {
  font-size: 12px;
  line-height: 2;
  overflow-wrap: anywhere;
}
.table-scroll {
  overflow-x: auto;
  margin: 16px 0;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  text-align: left;
}
td,
th {
  padding: 12px 10px;
  border-bottom: 1px solid #e4ece6;
  vertical-align: top;
  overflow-wrap: anywhere;
}
th {
  white-space: nowrap;
  background: #f6f9f7;
}
td:first-child {
  min-width: 150px;
}
.evidence {
  padding: 10px;
  background: #f6f9f7;
  margin-top: 8px;
}
.evidence p {
  margin: 4px 0;
}
footer {
  justify-content: flex-end;
  font-size: 12px;
}
@media (max-width: 700px) {
  .expansion-panel {
    padding: 16px;
  }
  .directions {
    grid-template-columns: 1fr;
  }
  .conditions label {
    width: 100%;
  }
  header {
    align-items: flex-start;
  }
}
</style>

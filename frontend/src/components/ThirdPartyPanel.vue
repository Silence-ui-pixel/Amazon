<script setup lang="ts">
import SourceExportsPanel from './SourceExportsPanel.vue';
import { usePanelSync } from '../panelSync';
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import {
  importFields,
  thirdPartyApi,
  type ImportPage,
  type ImportReport,
  type ImportWorkspace,
  type BrowserJob,
} from '../api/thirdParty';
import type { InputRun } from '../api/images';
import { periodLabel } from '../reportPeriod';
import AsinBatchSelector from './AsinBatchSelector';

const props = defineProps<{
  run: InputRun;
  syncRevision?: number;
  marketplace: string;
  coreReady: boolean;
  competitorsReady: boolean;
  coreSelectionId: string;
  competitorSelectionId: string;
}>();
const emit = defineEmits<{
  saved: [];
  status: [value: 'waiting' | 'running' | 'review' | 'done'];
}>();
const data = ref<ImportWorkspace | null>(null);
const direction = ref<'forward' | 'reverse'>('forward');
type CollectionMethod = 'keyword_mining' | 'keyword_reverse' | 'traffic_extension';
type VariantScope = 'all' | 'best_selling' | 'current';
const collectionMethod = ref<CollectionMethod>('keyword_mining');
const autoQuery = ref('');
const selectedAsins = ref<string[]>([]);
const variantScope = ref<VariantScope>('all');
const browserJobs = ref<BrowserJob[]>([]);
const collecting = ref(false);
const deferredRevision = ref(0);
let pollTimer: ReturnType<typeof setTimeout> | undefined;
let pollingEpoch = 0;
const hasActiveJobs = computed(() =>
  browserJobs.value.some((j) => ['queued', 'running'].includes(j.status)),
);
// 采集任务与导入报告分开读取：任务成功后仍需确认字段映射，不能直接当作来源可用。
async function loadJobs() {
  const current = epoch;
  const ticket = ++pollingEpoch;
  if (pollTimer) clearTimeout(pollTimer);
  try {
    const list = await thirdPartyApi.jobs(props.run);
    if (!alive || current !== epoch || ticket !== pollingEpoch) return;
    const completed = list.find(
      (j) =>
        j.status === 'succeeded' &&
        !browserJobs.value.some((old) => old.id === j.id && old.status === 'succeeded'),
    );
    browserJobs.value = list;
    if (completed) {
      await refresh(current);
      if (alive && current === epoch && completed.report_id && !mappingDirty.value && !file.value) {
        selectedId.value = completed.report_id;
        shareUnit.value = 'fraction';
      }
    }
  } catch (e) {
    if (alive && current === epoch && ticket === pollingEpoch)
      error.value = e instanceof Error ? e.message : '采集状态读取失败';
  }
  if (alive && current === epoch && ticket === pollingEpoch && hasActiveJobs.value)
    pollTimer = setTimeout(() => void loadJobs(), 3000);
}
async function collectBrowser() {
  if (collecting.value || !autoReady.value) return;
  const batch = collectionMethod.value === 'traffic_extension';
  const selectedQueries = batch ? selectedAsins.value : [autoQuery.value];
  if (!selectedQueries.length || selectedQueries.length > 20 || selectedQueries.some((q) => !q))
    return;
  const current = epoch;
  collecting.value = true;
  error.value = '';
  try {
    await thirdPartyApi.collect(props.run, {
      direction: collectionMethod.value === 'keyword_mining' ? 'forward' : 'reverse',
      query: batch ? '' : autoQuery.value,
      method: collectionMethod.value,
      queries: selectedQueries,
      variant_scope: batch ? variantScope.value : null,
      period: 'last_30_days',
    });
    if (alive && current === epoch) await loadJobs();
  } catch (e) {
    if (alive && current === epoch) error.value = e instanceof Error ? e.message : '采集启动失败';
  } finally {
    if (alive && current === epoch) collecting.value = false;
  }
}
const query = ref('');
const periodStart = ref('');
const periodEnd = ref('');
const importPeriod = ref<'last_30_days' | 'date_range'>('last_30_days');
const importCollectedAt = ref(new Date().toISOString().slice(0, 16));
const total = ref('');
const complete = ref(false);
const currency = ref('');
const file = ref<File | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const selectedId = ref('');
const mapping = ref<Record<string, string>>({});
const shareUnit = ref<'fraction' | 'percent'>('percent');
const busy = ref(false);
// Read-only restoration must not change saved execution status.
const reading = ref(true);
const mappingDirty = ref(false);
const error = ref('');
const rowError = ref('');
const rows = ref<ImportPage | null>(null);
const offset = ref(0);
let epoch = 0;
let rowEpoch = 0;
let refreshEpoch = 0;
let alive = true;
let batchKey = crypto.randomUUID();
onBeforeUnmount(() => {
  alive = false;
  pollingEpoch++;
  if (pollTimer) clearTimeout(pollTimer);
  epoch++;
  rowEpoch++;
});
const queries = computed(
  () =>
    (direction.value === 'forward' ? data.value?.forward_queries : data.value?.reverse_queries) ??
    [],
);
const autoQueries = computed(() =>
  collectionMethod.value === 'keyword_mining'
    ? (data.value?.forward_queries ?? [])
    : (data.value?.reverse_queries ?? []),
);
const selected = computed(() => data.value?.reports.find((r) => r.id === selectedId.value));
const ready = computed(
  () => props.coreReady && (direction.value === 'forward' || props.competitorsReady),
);
const autoReady = computed(
  () =>
    props.coreReady &&
    (collectionMethod.value === 'keyword_mining' || props.competitorsReady) &&
    (collectionMethod.value === 'traffic_extension'
      ? selectedAsins.value.length > 0 && selectedAsins.value.length <= 20
      : !!autoQuery.value),
);
const directionLabel = (r: ImportReport) => (r.metadata.direction === 'forward' ? '正向' : '反向');
const stateLabel = (r: ImportReport) =>
  r.stale
    ? '上游已变化 · 历史词表'
    : r.status === 'preview'
      ? '待确认映射'
      : r.complete
        ? '已确认 · 人工声明完整'
        : '已确认 · 完整性待补';
watch([file, direction, query, periodStart, periodEnd, total, complete, currency], () => {
  batchKey = crypto.randomUUID();
});
watch(queries, (list) => {
  if (!list.includes(query.value)) query.value = list[0] ?? '';
});
watch(
  autoQueries,
  (list) => {
    if (!list.includes(autoQuery.value)) autoQuery.value = list[0] ?? '';
    selectedAsins.value = selectedAsins.value.filter((asin) => list.includes(asin)).slice(0, 20);
  },
  { immediate: true },
);
watch(collectionMethod, () => {
  const list = autoQueries.value;
  autoQuery.value = list[0] ?? '';
  selectedAsins.value = [];
});
watch(selectedAsins, (list) => {
  if (list.length > 20) {
    selectedAsins.value = list.slice(0, 20);
    error.value = '拓展流量词每批最多选择 20 个竞对 ASIN';
  }
});
watch(
  [data, reading, busy, hasActiveJobs, error, () => props.coreReady, () => props.competitorsReady],
  () => {
    if (reading.value) return;
    emit(
      'status',
      busy.value || hasActiveJobs.value
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
async function refresh(current = epoch, preserveDraft = true) {
  reading.value = true;
  const request = ++refreshEpoch;
  try {
    const result = await thirdPartyApi.get(props.run);
    if (!alive || current !== epoch || request !== refreshEpoch) return;
    if (preserveDraft && (mappingDirty.value || file.value)) {
      deferredRevision.value++;
      return;
    }
    data.value = result;
    error.value = '';
    if (!result.reports.some((r) => r.id === selectedId.value))
      selectedId.value = result.reports[0]?.id ?? '';
  } catch (e) {
    if (alive && current === epoch && request === refreshEpoch)
      error.value = e instanceof Error ? e.message : '读取失败';
  } finally {
    if (alive && current === epoch && request === refreshEpoch) reading.value = false;
  }
}
watch(
  () => props.run.id,
  () => {
    epoch++;
    rowEpoch++;
    busy.value = false;
    collecting.value = false;
    pollingEpoch++;
    if (pollTimer) clearTimeout(pollTimer);
    data.value = null;
    selectedId.value = '';
    rows.value = null;
    error.value = '';
    file.value = null;
    if (fileInput.value) fileInput.value.value = '';
    browserJobs.value = [];
    selectedAsins.value = [];
    void refresh();
    void loadJobs();
  },
  { immediate: true },
);
function chooseFile(event: Event) {
  file.value = (event.target as HTMLInputElement).files?.[0] ?? null;
}
async function upload() {
  if (busy.value || !file.value) return;
  const sourceTotal = total.value.trim() === '' ? null : Number(total.value);
  if (sourceTotal !== null && (!Number.isSafeInteger(sourceTotal) || sourceTotal < 0)) {
    error.value = '来源总行数须为非负整数或留空';
    return;
  }
  if (
    importPeriod.value === 'date_range' &&
    (!periodStart.value || !periodEnd.value || periodStart.value > periodEnd.value)
  ) {
    error.value = '请填写有效报告日期范围';
    return;
  }
  if (file.value.size > 10 * 1024 * 1024) {
    error.value = '词表文件不能超过 10 MiB';
    return;
  }
  const current = epoch;
  refreshEpoch++;
  busy.value = true;
  error.value = '';
  try {
    const result = await thirdPartyApi.preview(props.run, file.value, {
      direction: direction.value,
      query: query.value,
      collection_method: direction.value === 'forward' ? 'keyword_mining' : 'keyword_reverse',
      source_queries: [query.value],
      variant_scope: null,
      marketplace: props.marketplace,
      period_kind: importPeriod.value,
      period_start: importPeriod.value === 'date_range' ? periodStart.value : null,
      period_end: importPeriod.value === 'date_range' ? periodEnd.value : null,
      collected_at:
        importPeriod.value === 'last_30_days'
          ? new Date(importCollectedAt.value + 'Z').toISOString()
          : null,
      source_total: sourceTotal,
      export_complete: complete.value,
      batch_key: batchKey,
      currency: currency.value || null,
    });
    if (!alive || current !== epoch) return;
    mappingDirty.value = false;
    await refresh(current, false);
    file.value = null;
    if (fileInput.value) fileInput.value.value = '';
    if (alive && current === epoch) selectedId.value = result.id;
  } catch (e) {
    if (alive && current === epoch) error.value = e instanceof Error ? e.message : '上传失败';
  } finally {
    if (alive && current === epoch) busy.value = false;
  }
}
watch(selected, (report, previous) => {
  if (mappingDirty.value && report?.id === previous?.id) return;
  mappingDirty.value = false;
  mapping.value = { ...(report?.confirmation?.mapping ?? report?.suggested_mapping ?? {}) };
  shareUnit.value =
    report?.confirmation?.share_unit ??
    (browserJobs.value.some((j) => j.report_id === report?.id) ? 'fraction' : 'percent');
  offset.value = 0;
  void loadRows();
});
async function confirm() {
  if (!selected.value || busy.value) return;
  const current = epoch;
  refreshEpoch++;
  const id = selected.value.id;
  busy.value = true;
  error.value = '';
  try {
    const chosen = Object.fromEntries(Object.entries(mapping.value).filter(([, v]) => v !== ''));
    await thirdPartyApi.confirm(props.run, id, chosen, shareUnit.value);
    if (alive && current === epoch) {
      batchKey = crypto.randomUUID();
      mappingDirty.value = false;
      await refresh(current, false);
      emit('saved');
    }
  } catch (e) {
    if (alive && current === epoch) error.value = e instanceof Error ? e.message : '确认失败';
  } finally {
    if (alive && current === epoch) busy.value = false;
  }
}
async function loadRows() {
  const current = ++rowEpoch;
  const report = selected.value;
  rows.value = null;
  rowError.value = '';
  if (!report || report.status !== 'confirmed') return;
  try {
    const result = await thirdPartyApi.rows(props.run, report.id, offset.value);
    if (alive && current === rowEpoch) rows.value = result;
  } catch (e) {
    if (alive && current === rowEpoch)
      rowError.value = e instanceof Error ? e.message : '词表读取失败';
  }
}
watch(offset, () => {
  void loadRows();
});
async function download() {
  const current = epoch;
  if (!selected.value) return;
  try {
    await thirdPartyApi.download(props.run, selected.value.id);
  } catch (e) {
    if (alive && current === epoch) error.value = e instanceof Error ? e.message : '下载失败';
  }
}
const percent = (v: string | null) =>
  v === null ? '待补数据' : `${(Number(v) * 100).toFixed(2)}%`;
const syncPending = usePanelSync(
  () => [
    props.syncRevision,
    props.coreSelectionId,
    props.competitorSelectionId,
    deferredRevision.value,
  ],
  () => busy.value || collecting.value || !!file.value || mappingDirty.value,
  async () => {
    await refresh(epoch, true);
    await loadJobs();
  },
);
</script>

<template>
  <section class="third-party-panel" aria-labelledby="third-party-title">
    <p v-if="syncPending" role="status" class="sync-notice">
      后台结果有更新；当前操作或未保存修改结束后将自动同步。也可使用面板的读取按钮重新加载。
    </p>
    <header>
      <div>
        <small>第五步 · 三方工具穷尽流量入口</small>
        <h2 id="third-party-title">卖家精灵正向 / 反向词表</h2>
        <p>下载每个核心大词、竞对 ASIN 的词表，再导入并核对字段；保留长尾词和每一条来源。</p>
      </div>
      <button
        :disabled="busy"
        @click="
          mappingDirty = false;
          refresh();
        "
      >
        读取导入历史
      </button>
    </header>
    <p v-if="data" class="coverage">
      当前周期完整词表：正向 {{ data.covered_forward }} / {{ data.forward_queries.length }} · 反向
      {{ data.covered_reverse }} / {{ data.reverse_queries.length }}
    </p>
    <p v-if="data?.complete && coreReady && competitorsReady" class="success">
      本周期所需正反向词表均已确认，第五步已完成。
    </p>
    <p class="hint">
      完整性以你核对的来源总行数和完整导出声明为依据。第三方提供的 ABA 月 /
      周排名独立保留，后续汇总时与 ABA 原始周报核对。
    </p>
    <a v-if="selected" :href="thirdPartyApi.originalUrl(run, selected.id)" download
      >下载当前批次采集数据文件</a
    >
    <fieldset :disabled="busy || collecting">
      <legend>从已登录浏览器自动获取</legend>
      <div class="form-grid">
        <label
          >采集方式<select v-model="collectionMethod" aria-label="自动获取采集方式">
            <option value="keyword_mining">核心词正向拓词</option>
            <option value="keyword_reverse">竞品关键词反查</option>
            <option value="traffic_extension">多 ASIN 拓展流量词</option>
          </select></label
        >
        <label v-if="collectionMethod !== 'traffic_extension'"
          >查询对象<select v-model="autoQuery" aria-label="自动获取查询对象">
            <option v-for="q in autoQueries" :key="q" :value="q">{{ q }}</option>
          </select></label
        >
        <label v-else
          >竞对 ASIN（已选 {{ selectedAsins.length }} / 20）
          <AsinBatchSelector v-model="selectedAsins" :options="autoQueries" :max="20" />
        </label>
        <label v-if="collectionMethod === 'traffic_extension'"
          >变体范围<select v-model="variantScope" aria-label="拓展流量词变体范围">
            <option value="all">全部变体</option>
            <option value="best_selling">畅销变体</option>
            <option value="current">当前变体</option>
          </select></label
        >
        <label>采集周期<input value="最近 30 天" readonly aria-label="自动获取采集周期" /></label>
      </div>
      <p class="hint">
        复用已登录的紫鸟店铺，按美国站“最近 30 天”查询并记录采集时间。多 ASIN 拓展每批最多 20
        个；自动导出后进入预览，仍需确认字段映射和完整性。查询使用账号的正常配额。
      </p>
      <button :disabled="collecting || !autoReady || hasActiveJobs" @click="collectBrowser">
        {{ collecting ? '正在提交' : '从浏览器获取并导入预览' }}
      </button>
      <p v-if="collectionMethod !== 'keyword_mining' && !competitorsReady" class="hint">
        请先保存第三步竞品名单。
      </p>
      <details v-if="browserJobs.length" class="job-history">
        <summary>
          采集任务与错误记录（{{ browserJobs.length }} 项，{{
            browserJobs.filter((job) => job.status === 'failed').length
          }}
          项失败）
        </summary>
        <p
          v-for="job in browserJobs"
          :key="job.id"
          class="browser-job"
          :role="job.error ? 'alert' : 'status'"
        >
          {{
            job.request.method === 'keyword_mining'
              ? '正向拓词'
              : job.request.method === 'keyword_reverse'
                ? '关键词反查'
                : '拓展流量词'
          }}
          ·
          {{
            job.request.method === 'traffic_extension'
              ? `${job.request.queries.length} 个 ASIN · ${
                  job.request.variant_scope === 'all'
                    ? '全部变体'
                    : job.request.variant_scope === 'best_selling'
                      ? '畅销变体'
                      : '当前变体'
                }`
              : job.request.query
          }}
          ·
          {{ job.request.period === 'last_30_days' ? '最近 30 天' : job.request.month }} —
          {{ job.stage }}
          <span v-if="job.error">{{ job.error }}</span>
          <button v-if="job.report_id" @click="selectedId = job.report_id">查看导入预览</button>
        </p>
      </details>
    </fieldset>
    <details class="manual-import">
      <summary>手动导入词表（已有导出文件时使用）</summary>
      <fieldset :disabled="busy">
        <legend>导入卖家精灵导出词表</legend>
        <div class="form-grid">
          <label
            >查找方向<select v-model="direction">
              <option value="forward">核心词正向查找</option>
              <option value="reverse">竞对 ASIN 反向查找</option>
            </select></label
          >
          <label
            >{{ direction === 'forward' ? '对应核心大词' : '对应竞对 ASIN'
            }}<select v-model="query">
              <option v-for="q in queries" :key="q" :value="q">{{ q }}</option>
            </select></label
          >
          <label
            >原表周期<select v-model="importPeriod">
              <option value="last_30_days">最近 30 天</option>
              <option value="date_range">固定日期（历史原表）</option>
            </select></label
          >
          <template v-if="importPeriod === 'date_range'">
            <label>报告开始日<input v-model="periodStart" type="date" /></label>
            <label>报告结束日<input v-model="periodEnd" type="date" /></label>
          </template>
          <label v-else
            >原表采集时间（UTC）<input v-model="importCollectedAt" type="datetime-local"
          /></label>
          <label
            >PPC 竞价原表币种<select v-model="currency">
              <option value="">未确认（暂不映射竞价）</option>
              <option
                v-for="code in ['USD', 'CAD', 'GBP', 'EUR', 'JPY', 'AUD', 'CNY']"
                :key="code"
                :value="code"
              >
                {{ code }}
              </option>
            </select></label
          >
          <label
            >来源总行数（不含表头）<input
              v-model="total"
              inputmode="numeric"
              placeholder="未知可留空，暂不计完整"
          /></label>
          <label
            >CSV / TSV / XLSX<input
              ref="fileInput"
              type="file"
              accept=".csv,.tsv,.xlsx"
              @change="chooseFile"
          /></label>
        </div>
        <label class="check"
          ><input
            v-model="complete"
            type="checkbox"
          />我已核对该查询的全部导出结果，文件未被筛选或截断</label
        >
        <p v-if="!ready" class="notice">请先保存当前核心大词；反向还需要保存竞对选择。</p>
        <p class="hint">
          每份文件一个查询、一个非空工作表；上限 10 MiB、20000
          行。保留原表头，不确定的字段可以在预览中调整映射。
        </p>
        <button :disabled="!ready || !query || !file" @click="upload">
          {{ busy ? '处理中…' : '上传并预览' }}
        </button>
      </fieldset>
    </details>
    <p v-if="error" class="notice" role="alert">{{ error }}</p>
    <div v-if="data?.reports.length" class="history">
      <details class="business-history">
        <summary>历史快照与版本选择</summary>
        <label
          >导入历史<select v-model="selectedId" :disabled="busy">
            <option v-for="r in data.reports" :key="r.id" :value="r.id">
              {{ directionLabel(r) }} · {{ r.metadata.query }} · {{ periodLabel(r.metadata) }} ·
              {{ stateLabel(r) }}
            </option>
          </select></label
        >
      </details>
    </div>
    <article v-if="selected">
      <h3>
        {{ directionLabel(selected) }} · {{ selected.metadata.query }}
        <small>{{ stateLabel(selected) }}</small>
      </h3>
      <p>
        {{ selected.filename }} · {{ selected.imported_rows }} 条来源行<span
          v-if="selected.status === 'confirmed'"
        >
          · {{ selected.unique_rows }} 个去重关键词</span
        >
        · {{ selected.metadata.marketplace
        }}<span v-if="selected.metadata.currency"> · {{ selected.metadata.currency }}</span> ·
        {{ periodLabel(selected.metadata) }}
      </p>
      <details>
        <summary>文件追溯</summary>
        <p class="digest">
          SHA256：{{ selected.file_sha256 }}<br />批次：{{ selected.id }}<br />创建：{{
            selected.created_at
          }}
        </p>
      </details>
      <template v-if="selected.status === 'preview'">
        <h4>核对字段映射</h4>
        <p class="hint">
          已按卖家精灵导出表头自动建议，请核对后确认。份额单位：10% 或数值 10 选择百分数；0.10
          选择小数。竞价须为纯数值，带币种符号时可先不映射。
        </p>
        <div class="form-grid" @change="mappingDirty = true">
          <label v-for="(label, key) in importFields" :key="key"
            >{{ label
            }}<select v-model="mapping[key]" :disabled="busy || selected.stale">
              <option value="">不导入此指标</option>
              <option v-for="h in selected.headers" :key="h" :value="h">{{ h }}</option>
            </select></label
          >
          <label
            >点击 / 转化份额原始单位<select v-model="shareUnit" :disabled="busy || selected.stale">
              <option value="percent">百分数（10 或 10%）</option>
              <option value="fraction">小数（0.10）</option>
            </select></label
          >
        </div>
        <div class="table-wrap">
          <table>
            <caption>
              原始前 5 行预览
            </caption>
            <thead>
              <tr>
                <th v-for="h in selected.headers" :key="h">{{ h }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(sample, i) in selected.samples" :key="i">
                <td v-for="h in selected.headers" :key="h">{{ sample[h] || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <button
          :disabled="
            busy ||
            selected.stale ||
            !mapping.keyword ||
            !coreReady ||
            (selected.metadata.direction === 'reverse' && !competitorsReady)
          "
          @click="confirm"
        >
          确认映射并保存词表
        </button>
      </template>
      <template v-else>
        <button :disabled="busy" @click="download">下载含来源的 CSV</button>
        <p v-if="!selected.complete" class="notice">
          此批次完整性待补：请核对总行数和完整声明，重新上传。已确认批次保留为历史。
        </p>
        <p v-if="rowError" role="alert" class="notice">
          {{ rowError }} <button @click="loadRows">重试读取</button>
        </p>
        <div v-if="rows" class="table-wrap">
          <table>
            <caption>
              来源明细（重复关键词保留各行证据）
            </caption>
            <thead>
              <tr>
                <th>原始行</th>
                <th>关键词 / 翻译</th>
                <th>月搜索量</th>
                <th>第三方月 / 周排名</th>
                <th>第 1 商品点击 / 转化</th>
                <th>原始字段</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in rows.items" :key="r.row_number">
                <td>{{ r.row_number }}</td>
                <td>
                  {{ r.keyword }}<small>{{ r.translation ?? '—' }}</small>
                </td>
                <td>{{ r.monthly_searches ?? '待补数据' }}</td>
                <td>{{ r.monthly_rank ?? '—' }} / {{ r.weekly_rank ?? '—' }}</td>
                <td>{{ percent(r.top1_click_share) }} / {{ percent(r.top1_conversion_share) }}</td>
                <td>
                  <details>
                    <summary>查看原文</summary>
                    <dl>
                      <template v-for="(v, h) in r.raw" :key="h"
                        ><dt>{{ h }}</dt>
                        <dd>{{ v || '—' }}</dd></template
                      >
                    </dl>
                  </details>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <nav v-if="rows" aria-label="第三方词表分页">
          <span>共 {{ rows.total }} 条 · 第 {{ Math.floor(offset / 20) + 1 }} 页</span
          ><button :disabled="offset === 0" @click="offset -= 20">上一页</button
          ><button :disabled="offset + 20 >= rows.total" @click="offset += 20">下一页</button>
        </nav>
      </template>
    </article>
    <p v-else-if="data" class="hint">尚未导入词表。请在卖家精灵下载对应查询的全部结果后上传。</p>
    <SourceExportsPanel :run="run" :kinds="['ss_forward', 'ss_reverse']" />
  </section>
</template>

<style scoped>
.third-party-panel {
  margin: 24px 0;
  padding: 24px;
  border: 1px solid #dce6e2;
  border-radius: 18px;
  background: #fff;
  min-width: 0;
}
header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}
h2 {
  margin: 7px 0;
  font-size: 21px;
}
h3 {
  font-size: 17px;
}
h3 small {
  display: block;
  margin-top: 8px;
  font-weight: 400;
}
p {
  color: #536861;
  line-height: 1.7;
  font-size: 13px;
}
small {
  color: #60796f;
}
.hint {
  color: #6b7c75;
}
.coverage {
  font-weight: 600;
}
.notice {
  color: #965218;
  background: #fff6e9;
  padding: 10px;
  border-radius: 8px;
}
.success {
  color: #176745;
  background: #eaf8ef;
  padding: 12px;
  border-radius: 8px;
}
fieldset {
  border: 1px solid #e1e9e5;
  border-radius: 12px;
  padding: 16px;
  min-width: 0;
  margin: 18px 0;
}
legend {
  padding: 0 8px;
  font-size: 14px;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 7px;
  color: #445e53;
  font-size: 13px;
  min-width: 0;
}
select,
input {
  min-width: 0;
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #d5e0da;
  border-radius: 7px;
  padding: 9px;
  background: #fff;
  color: #263f32;
}
.check {
  flex-direction: row;
  align-items: flex-start;
  margin: 16px 0;
  line-height: 1.7;
}
.check input {
  width: auto;
  margin-top: 4px;
}
:deep(.asin-batch-list) {
  display: grid;
  gap: 4px;
  max-height: 220px;
  overflow-y: auto;
  box-sizing: border-box;
  border: 1px solid #d5e0da;
  border-radius: 7px;
  padding: 8px;
  background: #fff;
}
:deep(.asin-batch-option) {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 8px;
  padding: 5px 7px;
  border-radius: 5px;
  cursor: pointer;
}
:deep(.asin-batch-option:hover) {
  background: #f1f8f4;
}
:deep(.asin-batch-option input) {
  width: auto;
  margin: 0;
}
:deep(.asin-batch-option:has(input:disabled)) {
  opacity: 0.5;
  cursor: default;
}
:deep(.asin-batch-empty) {
  padding: 8px;
  color: #6b7c75;
}
button {
  border: 1px solid #bfd5c8;
  border-radius: 8px;
  background: #f1f8f4;
  color: #245c40;
  padding: 9px 14px;
  font-size: 13px;
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
article {
  border-top: 1px solid #e5ece8;
  margin-top: 20px;
  padding-top: 10px;
  min-width: 0;
}
.history {
  margin: 20px 0;
}
.digest {
  overflow-wrap: anywhere;
}
summary {
  cursor: pointer;
  font-size: 12px;
  color: #35684e;
}
.table-wrap {
  overflow-x: auto;
  max-width: 100%;
  margin: 16px 0;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  min-width: 720px;
}
caption {
  text-align: left;
  padding: 10px 0;
  color: #60776b;
}
th,
td {
  padding: 10px;
  border-bottom: 1px solid #e7eee9;
  text-align: left;
  vertical-align: top;
  max-width: 250px;
  overflow-wrap: anywhere;
}
th {
  background: #f6f9f7;
  white-space: nowrap;
}
td small {
  display: block;
  margin-top: 5px;
}
dl {
  width: 180px;
}
dt {
  font-weight: 600;
}
dd {
  margin: 4px 0 9px;
}
nav {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  font-size: 12px;
  color: #64796d;
}
@media (max-width: 700px) {
  .third-party-panel {
    padding: 16px;
  }
  header {
    flex-direction: column;
  }
  .form-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>

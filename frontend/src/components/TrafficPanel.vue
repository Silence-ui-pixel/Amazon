<script setup lang="ts">
import { usePanelSync } from '../panelSync';
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { cleaningApi, type CleaningReport } from '../api/cleaning';
import {
  trafficApi,
  type TrafficReport,
  type TrafficReviewProgress,
  type TrafficRow,
  type TrafficReview,
  type TrafficPage,
  type ReviewRecord,
} from '../api/traffic';
import type { InputRun } from '../api/images';
import BatchTrafficPanel from './BatchTrafficPanel.vue';
import { periodLabel } from '../reportPeriod';

const props = defineProps<{ run: InputRun; ready: boolean; sourceRevision: number }>();
const emit = defineEmits<{
  saved: [];
  status: [value: 'waiting' | 'running' | 'review' | 'done'];
}>();
const cleanings = ref<CleaningReport[]>([]);
const sourceReports = ref<CleaningReport[]>([]);
const cleaningId = ref('');
const history = ref<TrafficReport[]>([]);
const selectedId = ref('');
const report = computed(() => history.value.find((r) => r.id === selectedId.value));
const sourceReport = computed(() =>
  sourceReports.value.find((c) => c.id === report.value?.request.cleaning_id),
);
const page = ref<TrafficPage | null>(null);
const offset = ref(0);
const keyword = ref('');
const row = computed(() => page.value?.items.find((r) => r.phrase.keyword === keyword.value));
const audits = ref<ReviewRecord[]>([]);
const reviewBox = ref<HTMLElement | null>(null);
const busy = ref(false);
// Read-only restoration must not change saved execution status.
const reading = ref(true);
const dirty = ref(false);
const batchDirty = ref(false);
const batchReset = ref(0);
const error = ref('');
const savingReview = ref(false);
const reviewNotice = ref('');
const reviewError = ref('');
const reviewProgress = ref<TrafficReviewProgress | null>(null);
const progressError = ref('');
let progressTimer: ReturnType<typeof setTimeout> | undefined;
const reviewRunning = computed(
  () =>
    reviewProgress.value?.analysis_id === selectedId.value &&
    ['queued', 'running'].includes(reviewProgress.value.status),
);
const progressPercent = computed(() => {
  const p = reviewProgress.value;
  return p
    ? p.required
      ? Math.min(100, Math.floor((p.reviewed / p.required) * 100))
      : p.complete
        ? 100
        : 0
    : 0;
});
const progressTitle = computed(() => {
  if (!reviewProgress.value) return '';
  return {
    idle: '尚未启动模型审核',
    queued: '审核任务已排队',
    running: '大模型正在审核',
    succeeded: '模型审核已完成',
    failed: '模型审核已暂停',
  }[reviewProgress.value.status];
});
// 草稿绑定读取时的审核版本；保存交给后端做冲突校验，不能刷新版本号后覆盖旧草稿。
const draftVersion = ref(0);
const style = ref('unknown');
const price = ref<string | number>('');
const share = ref<string | number>('');
const decision = ref<TrafficReview['decision']>('defer');
const priority = ref<TrafficReview['priority']>('normal');
const reason = ref('');
const evidence = ref('');
const reviewer = ref('');
let epoch = 0;
let alive = true;
onBeforeUnmount(() => {
  alive = false;
  epoch++;
  clearTimeout(progressTimer);
});
const message = (e: unknown) => (e instanceof Error ? e.message : '操作失败，请重试');
const flag = (v: boolean | null) => (v === null ? '未知' : v ? '是' : '否');
const percent = (v: string | null) => (v === null ? '未知' : `${(Number(v) * 100).toFixed(2)}%`);
const num = (v: number | null) => (v === null ? '—' : v.toLocaleString());
const sourceName = (provider: 'aba' | 'sellersprite', direction: 'forward' | 'reverse') =>
  `${provider === 'aba' ? 'ABA' : '卖家精灵'} ${direction === 'forward' ? '正向' : '反向'}`;
function restore(r: TrafficRow | undefined) {
  const d = r?.review?.data;
  draftVersion.value = report.value?.version ?? 0;
  style.value = d?.style_similar === true ? 'yes' : d?.style_similar === false ? 'no' : 'unknown';
  price.value =
    d?.price_delta_ratio === null || d?.price_delta_ratio === undefined
      ? ''
      : String(Number(d.price_delta_ratio) * 100);
  share.value =
    d?.first_half_result_share === null || d?.first_half_result_share === undefined
      ? ''
      : String(Number(d.first_half_result_share) * 100);
  decision.value = d?.decision ?? 'defer';
  priority.value = d?.priority ?? 'normal';
  reason.value = d?.reason ?? '';
  evidence.value = d?.evidence_note ?? '';
  reviewer.value = d?.reviewer ?? '';
  dirty.value = false;
}
function editReview() {
  dirty.value = true;
  reviewNotice.value = '';
  reviewError.value = '';
}
const current = (token: number) => alive && token === epoch;
async function loadRows(token = epoch) {
  if (!report.value) {
    page.value = null;
    return;
  }
  const id = report.value.id;
  const start = offset.value;
  const result = await trafficApi.rows(props.run, id, start);
  if (!current(token) || selectedId.value !== id || offset.value !== start) return;
  page.value = result;
  if (!page.value.items.some((r) => r.phrase.keyword === keyword.value))
    keyword.value = page.value.items[0]?.phrase.keyword ?? '';
}
function scheduleProgress() {
  clearTimeout(progressTimer);
  if (alive && report.value && (reviewRunning.value || progressError.value))
    progressTimer = setTimeout(() => void pollProgress(), 3000);
}
async function readProgress(token = epoch) {
  const id = selectedId.value;
  if (!id) {
    reviewProgress.value = null;
    return;
  }
  const result = await trafficApi.progress(props.run, id);
  if (!current(token) || selectedId.value !== id) return;
  const wasRunning = reviewRunning.value;
  reviewProgress.value = result;
  progressError.value = '';
  // 进度来自已保存审核数。草稿存在时只更新进度，保留编辑时绑定的审核版本。
  if (!dirty.value && !batchDirty.value) {
    const changed = report.value?.version !== result.version;
    history.value = history.value.map((r) =>
      r.id === id
        ? {
            ...r,
            reviewed: result.reviewed,
            required: result.required,
            total: result.total,
            version: result.version,
            complete: result.complete,
            stale: result.stale,
          }
        : r,
    );
    if (changed) {
      await loadRows(token);
      if (current(token) && !dirty.value && !batchDirty.value) restore(row.value);
    }
  }
  if (wasRunning && result.status === 'succeeded') emit('saved');
  scheduleProgress();
}
async function pollProgress() {
  if (busy.value) {
    scheduleProgress();
    return;
  }
  const token = epoch;
  try {
    await readProgress(token);
  } catch (e) {
    if (current(token)) progressError.value = `进度读取失败，正在自动重连：${message(e)}`;
  } finally {
    scheduleProgress();
  }
}
async function startModelReview() {
  if (!report.value || reviewRunning.value || busy.value || dirty.value || batchDirty.value) return;
  const id = report.value.id;
  const token = ++epoch;
  busy.value = true;
  error.value = '';
  try {
    const result = await trafficApi.startReview(props.run, id);
    if (!current(token) || selectedId.value !== id) return;
    reviewProgress.value = result;
    await readProgress(token);
  } catch (e) {
    if (current(token)) error.value = message(e);
  } finally {
    if (current(token)) busy.value = false;
    scheduleProgress();
  }
}
async function refresh(discard = true) {
  reading.value = true;
  if (discard) {
    batchReset.value++;
    batchDirty.value = false;
    reviewNotice.value = '';
    reviewError.value = '';
  }
  const token = ++epoch;
  busy.value = true;
  error.value = '';
  try {
    const [cs, hs] = await Promise.all([
      cleaningApi.history(props.run),
      trafficApi.history(props.run),
    ]);
    if (!alive || token !== epoch) return;
    sourceReports.value = cs;
    cleanings.value = cs.filter((c) => c.review && !c.stale);
    if (!cleanings.value.some((c) => c.id === cleaningId.value))
      cleaningId.value = cleanings.value[0]?.id ?? '';
    history.value = hs;
    if (!hs.some((r) => r.id === selectedId.value)) selectedId.value = hs[0]?.id ?? '';
    await loadRows(token);
    if (!alive || token !== epoch) return;
    if (discard || !dirty.value) restore(row.value);
    audits.value = [];
    await readProgress(token);
  } catch (e) {
    if (current(token)) error.value = message(e);
  } finally {
    if (current(token)) { busy.value = false; reading.value = false; }
    scheduleProgress();
  }
}
async function generate() {
  const c = cleanings.value.find((c) => c.id === cleaningId.value);
  if (!c) return;
  const token = ++epoch;
  busy.value = true;
  error.value = '';
  try {
    const r = await trafficApi.create(props.run, c.id, c.version);
    if (!current(token)) return;
    history.value = [r, ...history.value.filter((x) => x.id !== r.id)];
    selectedId.value = r.id;
    offset.value = 0;
    keyword.value = '';
    await loadRows(token);
    if (!current(token)) return;
    restore(row.value);
    audits.value = [];
    await readProgress(token);
  } catch (e) {
    if (current(token)) error.value = message(e);
  } finally {
    if (current(token)) busy.value = false;
    scheduleProgress();
  }
}
async function selectSnapshot() {
  audits.value = [];
  offset.value = 0;
  keyword.value = '';
  await navigate();
}
async function navigate() {
  const token = ++epoch;
  busy.value = true;
  error.value = '';
  try {
    await loadRows(token);
    if (current(token)) restore(row.value);
    await readProgress(token);
  } catch (e) {
    if (current(token)) error.value = message(e);
  } finally {
    if (current(token)) busy.value = false;
  }
}
async function choose(r: TrafficRow) {
  reviewNotice.value = '';
  reviewError.value = '';
  keyword.value = r.phrase.keyword;
  restore(r);
  // 等待所选关键词的表单更新后定位，避免用户寻找表格下方的审核区。
  await nextTick();
  reviewBox.value?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  reviewBox.value?.focus({ preventScroll: true });
}
function cancelReview() {
  restore(row.value);
  reviewNotice.value = '';
  reviewError.value = '';
}
async function batchSaved(r: TrafficReport) {
  history.value = history.value.map((x) => (x.id === r.id ? r : x));
  batchDirty.value = false;
  batchReset.value++;
  await navigate();
  emit('saved');
}
async function save() {
  if (!report.value || !row.value) {
    reviewError.value = '当前关键词尚未加载，请重新选择后再保存。';
    return;
  }
  const token = ++epoch;
  busy.value = true;
  savingReview.value = true;
  error.value = '';
  reviewError.value = '';
  reviewNotice.value = '正在保存当前关键词审核…';
  try {
    const r = await trafficApi.review(props.run, report.value.id, {
      expected_version: draftVersion.value,
      keyword: keyword.value,
      decision: decision.value,
      priority: priority.value,
      style_similar: style.value === 'unknown' ? null : style.value === 'yes',
      price_delta_ratio:
        String(price.value).trim() === '' ? null : String(Number(price.value) / 100),
      first_half_result_share:
        String(share.value).trim() === '' ? null : String(Number(share.value) / 100),
      reason: reason.value,
      evidence_note: evidence.value,
      reviewer: reviewer.value,
    });
    if (!current(token)) return;
    history.value = history.value.map((x) => (x.id === r.id ? r : x));
    await loadRows(token);
    if (!current(token)) return;
    restore(row.value);
    reviewNotice.value = '已保存当前关键词审核。';
    audits.value = [];
    emit('saved');
  } catch (e) {
    if (current(token)) {
      reviewNotice.value = '';
      reviewError.value = message(e);
      error.value = reviewError.value;
    }
  } finally {
    if (current(token)) {
      busy.value = false;
      savingReview.value = false;
    }
  }
}
async function download() {
  if (!report.value) return;
  try {
    await trafficApi.download(props.run, report.value.id);
  } catch (e) {
    error.value = message(e);
  }
}
async function readAudits() {
  if (!report.value) return;
  const token = epoch;
  const id = report.value.id;
  try {
    const result = await trafficApi.reviews(props.run, id);
    if (current(token) && selectedId.value === id) audits.value = result;
  } catch (e) {
    error.value = message(e);
  }
}
watch(
  () => props.run.id,
  () => {
    clearTimeout(progressTimer);
    reviewProgress.value = null;
    progressError.value = '';
    void refresh(false);
  },
  { immediate: true },
);
watch(
  [report, reading, busy, dirty, batchDirty, error, reviewRunning, () => props.ready],
  () => {
    if (reading.value) return;
    return emit(
      'status',
      busy.value || reviewRunning.value
        ? 'running'
        : report.value?.complete &&
            !report.value.stale &&
            !dirty.value &&
            !batchDirty.value &&
            !error.value
          ? 'done'
          : report.value
            ? 'review'
            : 'waiting',
    );
  },
  { immediate: true },
);
const syncPending = usePanelSync(
  () => [props.sourceRevision, props.ready],
  () => busy.value || dirty.value || batchDirty.value,
  async () => {
    selectedId.value = '';
    await refresh(false);
  },
);
</script>

<template>
  <section class="traffic-panel" aria-label="筛选流量入口">
    <p v-if="syncPending" role="status" class="sync-notice">
      后台结果有更新；当前操作或未保存修改结束后将自动同步。也可使用面板的读取按钮重新加载。
    </p>
    <header>
      <div>
        <p class="eyebrow">STEP 07 · 流量筛选</p>
        <h2>筛选流量入口</h2>
        <p>对照同周 ABA 指标，逐词核对相关性与投放优先级。</p>
      </div>
      <button :disabled="busy" @click="refresh()">读取筛选历史（丢弃未保存修改）</button>
    </header>
    <p v-if="!ready && !report?.complete" class="notice">
      先完成第六步人工确认；历史筛选仍可查看。
    </p>
    <div class="tools">
      <label
        >已审核的清洗快照<select v-model="cleaningId" :disabled="busy || dirty || batchDirty">
          <option value="">请选择</option>
          <option v-for="c in cleanings" :key="c.id" :value="c.id">
            {{ c.created_at }} · 第 {{ c.version }} 版 · {{ c.phrase_count }} 词
          </option>
        </select></label
      ><button
        class="primary"
        :disabled="busy || reviewRunning || !ready || !cleaningId || dirty || batchDirty"
        @click="generate"
      >
        生成流量筛选
      </button>
    </div>
    <p class="muted">
      点击生成后自动进行大模型审核，每批完成立即保存。购买比仅辅助分析顺序；不自动排除非购买型、垄断词或长尾词。第三方指标不代替
      ABA 周报。
    </p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <details class="business-history">
      <summary>历史快照与版本选择</summary>
      <label v-if="history.length"
        >筛选快照<select
          v-model="selectedId"
          :disabled="busy || dirty || batchDirty"
          @change="selectSnapshot"
        >
          <option v-for="r in history" :key="r.id" :value="r.id">
            {{ r.created_at }} · {{ r.stale ? '上游已变化' : r.complete ? '已审核' : '待审核' }}
          </option>
        </select></label
      >
    </details>
    <article v-if="report">
      <div v-if="reviewProgress" class="model-progress" role="status" aria-live="polite">
        <strong>{{ progressTitle }}</strong>
        <p>
          已审核 {{ reviewProgress.reviewed }} / {{ reviewProgress.required }} 词（{{
            progressPercent
          }}%）
        </p>
        <progress
          aria-label="模型审核进度"
          :value="reviewProgress.reviewed"
          :max="reviewProgress.required || 1"
        />
        <p v-if="reviewRunning" class="muted">
          每批结果实时保存；刷新或切换页面后可以继续查看进度。
        </p>
        <p v-if="reviewProgress.error" class="error">
          {{ reviewProgress.error }}（{{ reviewProgress.error_code }}）
        </p>
        <p v-if="progressError" class="notice">{{ progressError }}</p>
        <button
          v-if="!report.complete && !report.stale"
          class="primary"
          :disabled="busy || reviewRunning || dirty || batchDirty"
          @click="startModelReview"
        >
          {{
            reviewRunning
              ? '模型审核中…'
              : reviewProgress.status === 'failed'
                ? '重试未审核词'
                : '启动模型审核'
          }}
        </button>
      </div>
      <p v-if="sourceReport" class="muted">
        ABA 周结束日：{{ sourceReport.request.aba_week_end }} · 卖家精灵周期：
        {{
          sourceReport.request.seller_period === 'last_30_days'
            ? '最近 30 天'
            : `${sourceReport.request.seller_start} ～ ${sourceReport.request.seller_end}`
        }}（仅合并文本）
      </p>
      <p v-if="report.stale" class="notice">上游已变化，此筛选仅供追溯，请重新生成。</p>
      <p v-else-if="report.complete && !dirty && !batchDirty" class="success">
        全部候选已审核，第七步已完成。
      </p>
      <p>
        共 {{ report.total }} 词 · 已明确审核 {{ report.reviewed }} / {{ report.required }} 词 ·
        审核版本 {{ report.version }}
      </p>
      <p class="muted">
        规则 {{ report.rules.id }} · 点击最高商品转化份额 / 其点击份额
        {{ report.rules.id === 'sop-traffic-v2' ? '≥' : '>' }}
        {{ percent(report.rules.purchase_threshold) }}；首位转化 &gt;
        {{ percent(report.rules.monopoly_first) }} 或前三合计 &gt;
        {{ percent(report.rules.monopoly_three) }}。优先分析排名 ≤{{
          report.rules.deep_rank_max
        }}；后续词保留。
      </p>
      <p class="muted">
        相关性高须核对：款式相近、同类型商品价格差 ≤15%、前半页同类商品占比
        ≥50%。缺证据为待核对；购买型词先投，购买比不足或垄断词后投。
      </p>
      <p v-if="report.rules.id !== 'sop-traffic-v3'" class="notice">
        此为旧规则快照。请点击“生成流量筛选”应用购买比严格大于 60% 的当前规则，再完成审核。
      </p>
      <div class="tools">
        <button @click="download">下载流量筛选 CSV</button
        ><button @click="readAudits">查看审核历史</button>
      </div>
      <p v-if="busy">正在读取或保存…</p>
      <details v-if="page" class="source-metrics" open>
        <summary>
          四路来源计算明细（当前页
          {{ page.items.flatMap((item) => item.source_metrics).length }} 行）
        </summary>
        <p class="muted">
          每条来源行独立计算，不跨表拼接点击或转化份额。最终汇总、标准化和去重在第八步完成。
        </p>
        <div class="table-scroll">
          <table aria-label="四路来源计算明细">
            <thead>
              <tr>
                <th>来源</th>
                <th>关键词</th>
                <th>查询词 / ASIN</th>
                <th>来源行</th>
                <th>排名</th>
                <th>购买比</th>
                <th>前三转化累计</th>
                <th>购买性</th>
                <th>垄断性</th>
                <th>缺失 / 说明</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="item in page.items" :key="item.phrase.keyword">
                <tr
                  v-for="source in item.source_metrics"
                  :key="`${source.source_id}-${source.source_row}-${item.phrase.keyword}`"
                >
                  <td>{{ sourceName(source.provider, source.direction) }}</td>
                  <td>{{ item.phrase.keyword }}</td>
                  <td>{{ source.query }}</td>
                  <td>{{ source.source_row }}</td>
                  <td>{{ num(source.metrics.rank) }}</td>
                  <td>{{ percent(source.metrics.purchase_ratio) }}</td>
                  <td>{{ percent(source.metrics.conversion_sum3) }}</td>
                  <td>{{ flag(source.metrics.purchase) }}</td>
                  <td>{{ flag(source.metrics.monopoly) }}</td>
                  <td>{{ source.metrics.missing.join('；') || '数据完整' }}</td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
      </details>
      <h3 v-if="page" class="review-heading">集中审核关键词</h3>
      <p v-if="page" class="muted">
        同名词集中审核一次；上方各来源行和行内计算保持独立，不在本步生成最终关键词主记录。
      </p>
      <div v-if="page" class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>关键词</th>
              <th>ABA 周排名</th>
              <th>购买比 / 购买型</th>
              <th>垄断型</th>
              <th>人工结果</th>
              <th>审核操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="r in page.items"
              :key="r.phrase.keyword"
              :class="{ selected: r.phrase.keyword === keyword }"
            >
              <td>
                {{ r.phrase.keyword
                }}<small>{{ r.metrics.deep_analysis ? '优先分析' : '保留候选' }}</small>
              </td>
              <td>{{ r.metrics.rank ?? '未知' }}</td>
              <td>{{ percent(r.metrics.purchase_ratio) }} / {{ flag(r.metrics.purchase) }}</td>
              <td>{{ flag(r.metrics.monopoly) }}</td>
              <td>
                {{
                  r.cleaning_exclusion
                    ? '清洗已排除：' + r.cleaning_exclusion
                    : r.review
                      ? { keep: '保留', exclude: '排除', defer: '待补数据' }[r.review.data.decision]
                      : '待审核'
                }}
              </td>
              <td>
                <button
                  :disabled="busy || dirty || batchDirty"
                  :aria-label="`审核 ${r.phrase.keyword}`"
                  class="review-action"
                  @click="choose(r)"
                >
                  {{ r.review ? '修改审核' : '审核' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="page" class="pagination">
        <span>第 {{ Math.floor(offset / 20) + 1 }} 页 · 共 {{ page.total }} 词</span
        ><button
          :disabled="busy || dirty || batchDirty || offset === 0"
          @click="
            offset -= 20;
            navigate();
          "
        >
          上一页</button
        ><button
          :disabled="busy || dirty || batchDirty || offset + 20 >= page.total"
          @click="
            offset += 20;
            navigate();
          "
        >
          下一页
        </button>
      </div>
      <BatchTrafficPanel
        v-if="page && report"
        :key="report.id"
        :run="run"
        :report="report"
        :rows="page.items"
        :disabled="busy || dirty || !ready"
        :reset="batchReset"
        @dirty="batchDirty = $event"
        @saved="batchSaved"
      />
      <article
        v-if="row"
        ref="reviewBox"
        class="review-box"
        tabindex="-1"
        aria-label="关键词审核表单"
      >
        <h3>审核：{{ keyword }}</h3>
        <p class="muted">
          选择保留、排除或待补数据，填写审核人、证据与理由，再点击“保存当前关键词审核”。
        </p>
        <p v-for="m in row.metrics.missing" :key="m" class="notice">{{ m }}</p>
        <div v-if="row.references.length" class="reference">
          <h4>三方词表参考（不参与指标与分层）</h4>
          <p v-for="(item, i) in row.references" :key="i">
            {{ item.direction === 'forward' ? '正向' : '反向' }} · {{ item.query }} ·
            {{ periodLabel(item) }} · 周排名 {{ num(item.weekly_rank) }} · 月搜索量
            {{ num(item.monthly_searches) }}
          </p>
        </div>
        <p v-else class="muted">无三方词表记录，判定仅依据 ABA 周报与人工证据。</p>
        <details>
          <summary>原始来源证据（{{ row.phrase.evidence.length }} 条）</summary>
          <div v-for="(e, i) in row.phrase.evidence" :key="i">
            <p>{{ e.source_id }} · {{ e.query }} · 来源行 {{ e.source_row }}</p>
            <pre>{{ e.payload }}</pre>
          </div>
        </details>
        <p v-if="row.review">
          已保存相关性：{{
            { high: '高相关', low: '低相关', unknown: '未知' }[row.review.relevance]
          }}
          · {{ row.review.data.reviewer }} · {{ row.review.created_at }}
        </p>
        <p v-if="row.cleaning_exclusion" class="notice">此词已在清洗阶段排除，保留来源供追溯。</p>
        <fieldset
          :disabled="busy || batchDirty || report.stale || !ready || !!row.cleaning_exclusion"
          @input="editReview"
          @change="editReview"
        >
          <legend>人工相关性证据与决定</legend>
          <div class="fields">
            <label
              >款式相近<select v-model="style">
                <option value="unknown">待核对</option>
                <option value="yes">是</option>
                <option value="no">否</option>
              </select></label
            ><label
              >同类价格浮动（%）<input
                v-model="price"
                type="number"
                min="0"
                max="10000"
                step="any" /></label
            ><label
              >前半页同类占比（%）<input
                v-model="share"
                type="number"
                min="0"
                max="100"
                step="any" /></label
            ><label
              >筛选决定<select v-model="decision">
                <option value="defer">待补数据</option>
                <option value="keep">保留</option>
                <option value="exclude">排除</option>
              </select></label
            ><label
              >投放优先级<select v-model="priority">
                <option value="high">高</option>
                <option value="normal">普通</option>
                <option value="low">低</option>
              </select></label
            ><label>审核人（本地记录）<input v-model="reviewer" maxlength="100" /></label>
          </div>
          <label
            >页面与价格证据<textarea
              v-model="evidence"
              maxlength="2000"
              placeholder="记录观察时间、搜索页、样本数及价格依据"
            /></label
          ><label>筛选理由<textarea v-model="reason" maxlength="1000" /></label>
        </fieldset>
        <p class="muted">
          高相关要求款式相近、价格浮动 ≤{{ percent(report.rules.price_delta_max) }}、前半页同类占比
          ≥{{ percent(report.rules.result_share_min) }}；未填证据项显示未知。筛选决定由人工负责。
        </p>
        <button
          class="primary"
          :disabled="
            busy ||
            batchDirty ||
            report.stale ||
            !ready ||
            !!row.cleaning_exclusion ||
            !reason.trim() ||
            !evidence.trim() ||
            !reviewer.trim()
          "
          @click="save"
        >
          {{ savingReview ? '正在保存…' : '保存当前关键词审核' }}</button
        ><button :disabled="busy || !dirty" @click="cancelReview">撤销当前修改</button>
        <p v-if="reviewNotice" role="status" class="save-feedback success">{{ reviewNotice }}</p>
        <p v-if="reviewError" role="alert" class="save-feedback error">{{ reviewError }}</p>
      </article>
      <p v-if="dirty" class="notice">有未保存的审核修改，第七步等待重新确认。</p>
      <details v-if="audits.length" open>
        <summary>审核历史：{{ audits.length }} 次</summary>
        <p v-for="a in audits" :key="a.version">
          版本 {{ a.version }} · {{ a.data.keyword }} · {{ a.data.reviewer }} · {{ a.created_at }} ·
          {{ a.data.decision }} · {{ a.data.reason }} · {{ a.data.evidence_note }}
        </p>
      </details>
    </article>
  </section>
</template>

<style scoped>
.model-progress {
  margin: 18px 0;
  padding: 16px 20px;
  border: 1px solid #cbd5f5;
  border-radius: 10px;
  background: #f7f9ff;
}
.model-progress p {
  margin: 8px 0;
}
.model-progress progress {
  display: block;
  width: 100%;
  height: 12px;
  accent-color: #4f46e5;
}

.traffic-panel {
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 16px;
  padding: 24px;
  margin: 20px 0;
  color: #23334a;
  min-width: 0;
}
header,
.tools,
.pagination {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
}
h2 {
  margin: 4px 0 10px;
  font-size: 21px;
}
.eyebrow {
  color: #4f46e5;
  font-size: 11px;
  letter-spacing: 1px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 7px;
  font-size: 13px;
  margin: 10px 0;
  min-width: 0;
}
input,
select,
textarea {
  border: 1px solid #cbd5e1;
  border-radius: 7px;
  padding: 9px;
  font: inherit;
  max-width: 100%;
  box-sizing: border-box;
}
textarea {
  min-height: 70px;
}
button {
  border: 1px solid #cbd5e1;
  background: #fff;
  padding: 9px 13px;
  border-radius: 7px;
  color: #334155;
  cursor: pointer;
  font: inherit;
  font-size: 13px;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
.primary {
  background: #4f46e5;
  color: #fff;
  border-color: #4f46e5;
  margin: 12px 0;
}
.notice {
  color: #9a6700;
}
.success {
  color: #15803d;
}
.save-feedback {
  margin: 2px 0 10px;
  font-weight: 600;
}
.error {
  color: #b42318;
}
.muted,
small {
  font-size: 12px;
  color: #64748b;
  line-height: 1.7;
}
small {
  display: block;
}
.table-scroll {
  overflow: auto;
  max-height: 500px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  margin: 16px 0;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
th,
td {
  padding: 12px;
  text-align: left;
  border-bottom: 1px solid #edf0f4;
  vertical-align: top;
}
th {
  background: #f8fafc;
  white-space: nowrap;
}
tr.selected {
  background: #eef2ff;
}
.pagination {
  justify-content: flex-end;
  font-size: 12px;
}
.fields {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
fieldset {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  min-width: 0;
  margin: 16px 0;
  padding: 14px;
}
.review-action {
  white-space: nowrap;
  min-width: 64px;
  color: #4f46e5;
  border-color: #a5b4fc;
}
.review-box {
  scroll-margin-top: 80px;
  border-top: 1px solid #e2e8f0;
  margin-top: 20px;
  padding-top: 12px;
}
details {
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 11px;
}
.tools label {
  flex: 1;
}
@media (max-width: 600px) {
  .traffic-panel {
    padding: 16px;
  }
  .fields {
    grid-template-columns: 1fr;
  }
  table {
    min-width: 680px;
  }
  header button {
    width: 100%;
  }
  .tools {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>

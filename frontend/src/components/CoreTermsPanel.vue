<script setup lang="ts">
import SourceExportsPanel from './SourceExportsPanel.vue';
import { usePanelSync } from '../panelSync';
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue';
import { Database, Check, RefreshCw } from '@lucide/vue';
import type { InputRun } from '../api/images';
import { latestReportWeek } from '../reportPeriod';
import {
  coreApi,
  isHotAnalysis,
  type AnyCoreAnalysis,
  type HotSearchCoreItem,
  type HotSearchCollectionJob,
  type ExactIntentEvidence,
  type AbaReport,
  type CoreAnalysis,
  type CoreCandidate,
  type CoreSelection,
  type CoreTerm,
} from '../api/coreTerms';
const props = defineProps<{ run: InputRun; syncRevision?: number; marketplace: string }>();
const emit = defineEmits<{
  status: [value: 'waiting' | 'running' | 'review' | 'done'];
  selection: [id: string];
  saved: [];
}>();
// 周报候选、模型分析、已保存选择是三类独立结果；分析成功不等于已确认核心词。
const reports = ref<AbaReport[]>([]);
const report = ref<AbaReport | null>(null);
const hotReport = computed(
  () =>
    report.value?.query_mode === 'product_term' ||
    report.value?.rule_version === 'hot-search-core-v1' ||
    report.value?.rule_version === 'hot-search-core-v2',
);
const candidates = ref<CoreCandidate[]>([]);
const total = ref(0);
const offset = ref(0);
const pageSize = ref(10);
const displayOffset = ref(0);
const partial = ref(false);
const analyses = ref<CoreAnalysis[]>([]);
const analysis = ref<AnyCoreAnalysis | null>(null);
const hotAnalysis = computed(() =>
  analysis.value && isHotAnalysis(analysis.value) ? analysis.value : null,
);
const selection = ref<CoreSelection | null>(null);
const collectionJob = ref<HotSearchCollectionJob | null>(null);
watch(
  () => selection.value?.id,
  (id) => {
    if (id) emit('selection', id);
  },
);
const selected = ref<string[]>([]);
const brandInput = ref('');
const busy = ref('');
// Read-only restoration must not change saved execution status.
const reading = ref(true);
const error = ref('');
const dirty = ref(false);
let alive = true;
let collectionPoll: ReturnType<typeof setTimeout> | undefined;
onBeforeUnmount(() => {
  alive = false;
  if (collectionPoll) clearTimeout(collectionPoll);
});
const week = defineModel<string>('reportWeek', { required: true });
const brands = computed(() => [
  ...new Set(
    brandInput.value
      .split(/[,，;；\n]/)
      .map((x) => x.trim())
      .filter(Boolean),
  ),
]);
// 服务端品牌按大小写无关的有序集合保存，前端使用同一比较口径。
const sameBrands = (a: string[], b: string[]) => {
  const normalized = (values: string[]) => [...new Set(values.map((x) => x.toLowerCase()))].sort();
  return JSON.stringify(normalized(a)) === JSON.stringify(normalized(b));
};
const analysisValid = computed(() => {
  if (hotAnalysis.value)
    return (
      ['completed', 'incomplete'].includes(hotAnalysis.value.status) &&
      !hotAnalysis.value.stale &&
      !report.value?.stale &&
      report.value?.week_end === week.value &&
      hotAnalysis.value.report_id === report.value?.id &&
      sameBrands(hotAnalysis.value.own_brands_snapshot, brands.value)
    );
  const value = analysis.value;
  return (
    value &&
    !isHotAnalysis(value) &&
    value.status === 'succeeded' &&
    !value.stale &&
    !report.value?.stale &&
    report.value?.week_end === week.value &&
    value.report_id === report.value?.id &&
    ((value.source_analysis_ids?.length || 0) > 0 ||
      (value.request.offset === offset.value && value.request.limit === pageSize.value)) &&
    sameBrands(value.request.own_brands, brands.value)
  );
});
const saved = computed(
  () =>
    !!selection.value &&
    !selection.value.stale &&
    analysisValid.value &&
    selection.value.analysis_id === analysis.value?.id &&
    selection.value.items.length === selected.value.length &&
    selection.value.items.every((x) => selected.value.includes(x.id)),
);
type HotRow = HotSearchCoreItem & CoreCandidate;
const rows = computed<Array<CoreCandidate | HotRow>>(() =>
  hotAnalysis.value
    ? hotAnalysis.value.items.map((x) => ({
        ...x,
        id: x.candidate_id,
        matched_roots: x.source_product_terms,
        root_reference: false,
      }))
    : analysisValid.value && analysis.value && !isHotAnalysis(analysis.value)
      ? analysis.value.items
      : candidates.value,
);
const aggregateAnalysis = computed(
  () =>
    hotReport.value ||
    !!hotAnalysis.value ||
    (analysisValid.value &&
      analysis.value &&
      !isHotAnalysis(analysis.value) &&
      (analysis.value.source_analysis_ids?.length || 0) > 0),
);
const orderedRows = computed(() => {
  const selectedIds = new Set(selected.value);
  return rows.value
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      const selectedOrder = Number(selectedIds.has(b.row.id)) - Number(selectedIds.has(a.row.id));
      return selectedOrder || a.index - b.index;
    })
    .map(({ row }) => row);
});
const visibleRows = computed(() =>
  aggregateAnalysis.value
    ? orderedRows.value.slice(displayOffset.value, displayOffset.value + pageSize.value)
    : orderedRows.value,
);
const displayTotal = computed(() => (aggregateAnalysis.value ? rows.value.length : total.value));
const paginationOffset = computed(() =>
  aggregateAnalysis.value ? displayOffset.value : offset.value,
);
const currentPage = computed(() =>
  displayTotal.value ? Math.floor(paginationOffset.value / pageSize.value) + 1 : 0,
);
const pageCount = computed(() => Math.ceil(displayTotal.value / pageSize.value));
const canAnalyze = computed(
  () =>
    report.value &&
    !report.value.stale &&
    candidates.value.length > 0 &&
    props.marketplace === 'US' &&
    !busy.value &&
    (!analysis.value || hotAnalysis.value?.status === 'stale'),
);
const canSave = computed(
  () => analysisValid.value && selected.value.length > 0 && !busy.value && !saved.value,
);
watch([saved, reading, busy, error, report, analysis], () => {
    if (reading.value) return;
  if (alive)
    emit(
      'status',
      error.value
        ? 'review'
        : busy.value
          ? 'running'
          : saved.value
            ? 'done'
            : report.value
              ? 'review'
              : 'waiting',
    );
});
watch(brands, () => {
  analysis.value = null;
  selected.value = [];
});
const term = (row: CoreCandidate): CoreTerm | undefined =>
  'recommended' in row ? (row as CoreTerm) : undefined;
const hot = (row: CoreCandidate): HotRow | undefined =>
  'candidate_id' in row ? (row as HotRow) : undefined;
// Recommended IDs are the backend's eligibility decision; pagination never narrows this set.
const eligible = (row: CoreCandidate) =>
  hotAnalysis.value
    ? hotAnalysis.value.recommended_candidate_ids.includes(row.id)
    : !!term(row)?.recommended;
const manualFallback = computed(
  () =>
    !!hotAnalysis.value &&
    ['completed', 'incomplete'].includes(hotAnalysis.value.status) &&
    hotAnalysis.value.recommended_candidate_ids.length === 0 &&
    hotAnalysis.value.items.length > 0,
);
const selectable = (row: CoreCandidate) =>
  eligible(row) ||
  (!!hotAnalysis.value && manualFallback.value && hot(row)!.search_frequency_rank <= 250000);
const intentGates: Array<{ key: keyof ExactIntentEvidence['reasons']; label: string }> = [
  { key: 'object_match', label: '对象一致' },
  { key: 'purpose_match', label: '用途一致' },
  { key: 'product_level_match', label: '商品层级' },
  { key: 'modifiers_compatible', label: '修饰兼容' },
];
const percent = (value: string | null | undefined) =>
  value == null ? '—' : `${Number(value).toFixed(2)}%`;
const v2Rules = computed(
  () =>
    report.value?.rule_version === 'hot-search-core-v2' ||
    hotAnalysis.value?.rule_version === 'hot-search-core-v2',
);
const confidenceLabels = { none: '无可信度', low: '低可信', medium: '中可信', high: '高可信' };
const itemStatus = {
  waiting: '等待处理',
  running: '正在分析',
  completed: '分析完成',
  no_samples: '无有效样本',
  failed: '采集失败',
};
const exclusionLabels: Record<string, string> = {
  rank_above_250000: 'ABA 排名超过 250,000',
  intent_not_exact: '非精准意图',
  brand_not_generic: '品牌词不入选',
  no_valid_samples: '无有效样本',
  collection_failed: '采集失败',
};
function exclusionReasons(row: CoreCandidate): string[] {
  const item = hot(row);
  if (!item || eligible(row)) return [];
  const reasons = item.exclusion_reasons.map((x) =>
    x === 'relevance_not_above_60'
      ? v2Rules.value
        ? '相关度低于 50%'
        : '相关度未超过 60%'
      : exclusionLabels[x] || x,
  );
  if (item.search_frequency_rank > 250000) reasons.push('ABA 排名超过 250,000');
  if (
    item.summary?.relevance_percent != null &&
    (v2Rules.value
      ? Number(item.summary.relevance_percent) < 50
      : Number(item.summary.relevance_percent) <= 60)
  )
    reasons.push(v2Rules.value ? '相关度低于 50%' : '相关度未超过 60%');
  if (item.summary?.valid_samples === 0 || item.status === 'no_samples') reasons.push('无有效样本');
  if (
    item.intent_evidence &&
    (item.intent_evidence.category !== 'exact' ||
      intentGates.some((x) => !item.intent_evidence![x.key]))
  )
    reasons.push('非精准意图');
  if (item.brand_category !== 'generic') reasons.push(`${labels[item.brand_category]}不入选`);
  return [
    ...new Set(
      reasons.length
        ? reasons
        : [item.status === 'completed' ? '证据不足，未获后端推荐' : itemStatus[item.status]],
    ),
  ];
}
const progress = computed(
  () =>
    hotAnalysis.value?.items.filter((x) => ['completed', 'failed', 'no_samples'].includes(x.status))
      .length || 0,
);
const labels: Record<string, string> = {
  generic: '通用词',
  own_brand: '自家品牌',
  competitor_brand: '竞品品牌',
  brand_unknown: '品牌待核对',
  high: '高相关',
  medium: '中相关',
  low: '低相关',
  exact: '精准意图',
  broad: '宽泛意图',
  accessory: '配件意图',
  unrelated: '不相关',
};
const statusText = computed(() => {
  if (hotAnalysis.value)
    return {
      queued: '分析已落盘；可继续执行已排队分析。',
      running: '逐词进度：正在处理搜索词。',
      completed: '',
      incomplete: '分析未完成，请检查逐词失败与无样本原因。',
      failed: '分析失败，请检查逐词结果。',
      stale: '历史结果已失效，请重新分析。',
    }[hotAnalysis.value.status];
  return {
    running: '分析处理中，请读取历史状态；不要重复提交。',
    outcome_pending: '模型结果待确认，可能已计费。请读取历史状态，不要重复提交。',
    failed: '本批分析失败，请查看错误。相同请求不会自动重试。',
    succeeded: '',
  }[(analysis.value as CoreAnalysis | null)?.status || 'succeeded'];
});
const collectionStatusLabels = {
  queued: '候选排队中',
  collecting: '候选采集中',
  completed: '候选完成',
  failed: '候选失败',
};
const exportStatusLabels = {
  queued: '表格排队中',
  downloading: '表格同步中',
  parsing: '表格解析中',
  verifying: '表格核对中',
  completed: '表格完成',
  download_failed: '表格下载失败',
  parse_failed: '表格解析失败',
  verification_failed: '表格核对失败',
};
const collectionActive = (value: HotSearchCollectionJob | null) =>
  !!value &&
  (['queued', 'collecting'].includes(value.status) ||
    value.tasks.some((task) =>
      ['queued', 'downloading', 'parsing', 'verifying'].includes(task.export_status),
    ));
function scheduleCollectionPoll() {
  if (collectionPoll) clearTimeout(collectionPoll);
  if (!alive || !collectionActive(collectionJob.value)) return;
  collectionPoll = setTimeout(async () => {
    if (!alive || !collectionJob.value) return;
    try {
      const value = await coreApi.hotCollection(props.run, collectionJob.value.id);
      if (!alive) return;
      collectionJob.value = value;
      if (value.report_id && report.value?.id !== value.report_id) {
        const published = await coreApi.report(props.run, value.report_id);
        reports.value = [published, ...reports.value.filter((item) => item.id !== published.id)];
        await loadReport(published.id, true);
      }
    } catch (e) {
      if (alive) error.value = e instanceof Error ? e.message : '读取商品词采集状态失败';
    } finally {
      scheduleCollectionPoll();
    }
  }, 2500);
}
// 所有操作使用同一忙碌锁，防止切报告、换页与计费请求互相覆盖。
async function perform(label: string, action: () => Promise<void>) {
  if (busy.value) return;
  reading.value = action === restore;
  busy.value = label;
  error.value = '';
  try {
    await action();
  } catch (e) {
    if (alive) error.value = e instanceof Error ? e.message : '请求失败';
  } finally {
    if (alive) { busy.value = ''; reading.value = false; }
  }
}
function applyAnalysis(value: AnyCoreAnalysis) {
  analysis.value = value;
  displayOffset.value = 0;
  selected.value =
    selection.value?.analysis_id === value.id && !selection.value.stale
      ? selection.value.items.map((x) => x.id)
      : isHotAnalysis(value)
        ? [...value.recommended_candidate_ids]
        : [];
}
async function loadPage(restoreBrands = false) {
  if (!report.value) return;
  displayOffset.value = 0;
  candidates.value = [];
  analysis.value = null;
  selected.value = [];
  analyses.value = [];
  total.value = 0;
  if (hotReport.value) {
    const latest = await coreApi.hotLatest(props.run);
    const matching = latest?.report_id === report.value.id ? latest : null;
    const items = matching
      ? matching.items
      : report.value.stale
        ? []
        : await coreApi.hotCandidates(props.run, report.value.id);
    candidates.value = items.map((x) => ({
      ...x,
      id: x.candidate_id,
      matched_roots: x.source_product_terms,
      root_reference: false,
    }));
    total.value = items.length;
    partial.value = matching?.source_partial ?? report.value.reports.some((x) => x.truncated);
    if (matching && (restoreBrands || sameBrands(matching.own_brands_snapshot, brands.value))) {
      if (restoreBrands) {
        brandInput.value = matching.own_brands_snapshot.join(', ');
        await Promise.resolve();
      }
      applyAnalysis(matching);
    }
    return;
  }
  const [page, history] = await Promise.all([
    coreApi.candidates(props.run, report.value.id, offset.value, pageSize.value),
    coreApi.analyses(props.run, report.value.id),
  ]);
  candidates.value = page.items;
  total.value = page.total;
  partial.value = page.source_partial;
  analyses.value = history;
  const match = history.find(
    (x) =>
      !(x.source_analysis_ids?.length || 0) &&
      x.request.offset === offset.value &&
      x.request.limit === pageSize.value &&
      sameBrands(x.request.own_brands, brands.value),
  );
  if (match) applyAnalysis(match);
}
async function loadReport(id: string, restoreBrands = false) {
  report.value = null;
  candidates.value = [];
  analysis.value = null;
  selected.value = [];
  report.value = await coreApi.report(props.run, id);
  offset.value = 0;
  await loadPage(restoreBrands);
}
async function restore() {
  const [history, confirmed, latest, latestCollection] = await Promise.all([
    coreApi.reports(props.run),
    coreApi.selection(props.run),
    coreApi.hotLatest(props.run),
    coreApi.hotCollectionLatest(props.run),
  ]);
  reports.value = history.filter((x) => x.query_mode === 'root' || x.query_mode === 'product_term');
  selection.value = confirmed;
  collectionJob.value = latestCollection;
  scheduleCollectionPoll();
  // 当前热门分析优先；失效或来自较早分析的确认仅提供版本背景。
  if (latest) {
    await loadReport(latest.report_id, true);
  } else if (
    confirmed &&
    !confirmed.stale &&
    !['hot-search-core-v1', 'hot-search-core-v2'].includes(confirmed.rule_version ?? '')
  ) {
    const existing = await coreApi.analysis(props.run, confirmed.analysis_id);
    brandInput.value = existing.request.own_brands.join(', ');
    if (!(existing.source_analysis_ids?.length || 0)) pageSize.value = existing.request.limit;
    await loadReport(confirmed.report_id);
    offset.value = existing.request.offset;
    await loadPage();
    applyAnalysis(existing);
  } else if (reports.value[0]) await loadReport(reports.value[0].id, true);
  dirty.value = false;
}
const syncPending = usePanelSync(
  () => props.syncRevision,
  () => !!busy.value || dirty.value,
  () => perform('同步编排结果', restore),
);
onMounted(() => {
  void perform('读取已保存结果', restore);
});
function collect() {
  void perform('获取 ABA 周报', async () => {
    const roots = await coreApi.roots(props.run);
    if (roots.rule_version === 'product-term-v1') {
      const job = await coreApi.startHotCollection(props.run);
      collectionJob.value = job;
      week.value = job.week_end;
      scheduleCollectionPoll();
      if (job.report_id) {
        const published = await coreApi.report(props.run, job.report_id);
        reports.value = [published, ...reports.value.filter((x) => x.id !== published.id)];
        await loadReport(published.id);
      }
      return;
    }
    const value = await coreApi.collect(props.run, week.value, 'root');
    week.value = value.week_end;
    reports.value = [value, ...reports.value.filter((x) => x.id !== value.id)];
    await loadReport(value.id);
  });
}
function retryCollectionTerm(productTerm: string) {
  if (!collectionJob.value) return;
  void perform(`重试 ${productTerm}`, async () => {
    collectionJob.value = await coreApi.retryHotCollectionTerm(
      props.run,
      collectionJob.value!.id,
      productTerm,
    );
    scheduleCollectionPoll();
    if (collectionJob.value.report_id) await restore();
  });
}
function analyze() {
  if (!canAnalyze.value || !report.value) return;
  void perform('分析候选（请等待）', async () => {
    const value = hotReport.value
      ? await coreApi.analyzeHot(props.run, report.value!.id, brands.value)
      : await coreApi.analyze(
          props.run,
          report.value!.id,
          offset.value,
          pageSize.value,
          brands.value,
        );
    if (!isHotAnalysis(value))
      analyses.value = [value, ...analyses.value.filter((x) => x.id !== value.id)];
    applyAnalysis(value);
    if (isHotAnalysis(value) && value.status === 'queued') await executeAndRead(value.id);
  });
}
async function executeAndRead(id: string) {
  applyAnalysis(await coreApi.executeHot(props.run, id));
  // Only this explicit action dispatches. Polls and refreshes are read-only.
  for (let attempt = 0; alive && attempt < 120; attempt++) {
    const value = await coreApi.hotAnalysis(props.run, id);
    if (!alive) return;
    applyAnalysis(value);
    if (!['queued', 'running'].includes(value.status)) return;
    await new Promise((resolve) => setTimeout(resolve, 2500));
  }
}
function executeQueued() {
  if (hotAnalysis.value?.status !== 'queued' || busy.value) return;
  const id = hotAnalysis.value.id;
  void perform('执行已排队分析', () => executeAndRead(id));
}
function save() {
  if (!canSave.value || !analysis.value) return;
  void perform('保存核心大词', async () => {
    selection.value = await coreApi.save(
      props.run,
      analysis.value!.id,
      rows.value.filter((x) => selected.value.includes(x.id) && selectable(x)).map((x) => x.id),
      selection.value?.version || 0,
      hotAnalysis.value?.rule_version ?? 'core-terms-v1',
    );
    dirty.value = false;
    emit('saved');
  });
}
function changeReport(event: Event) {
  const id = (event.target as HTMLSelectElement).value;
  if (id) void perform('读取周报', () => loadReport(id));
}
function pageBy(delta: number) {
  if (aggregateAnalysis.value) {
    displayOffset.value += delta * pageSize.value;
    return;
  }
  void perform('读取候选', async () => {
    offset.value += delta * pageSize.value;
    await loadPage();
  });
}
function changePageSize() {
  displayOffset.value = 0;
  if (aggregateAnalysis.value) return;
  void perform('调整分页', async () => {
    offset.value = 0;
    await loadPage();
  });
}
function chooseAnalysis(event: Event) {
  const value = analyses.value.find((x) => x.id === (event.target as HTMLSelectElement).value);
  if (!value) return;
  void perform('读取分析历史', async () => {
    brandInput.value = value.request.own_brands.join(', ');
    if (!(value.source_analysis_ids?.length || 0)) pageSize.value = value.request.limit;
    // 等待品牌输入 watcher 失效旧分析，再应用所选历史。
    await Promise.resolve();
    offset.value = value.request.offset;
    await loadPage();
    applyAnalysis(await coreApi.analysis(props.run, value.id));
  });
}
function refresh() {
  void perform('读取历史状态', async () => {
    await restore();
  });
}
</script>

<template>
  <section
    class="core-panel"
    aria-label="确定核心大词"
    @input="dirty = true"
    @change="dirty = true"
  >
    <p v-if="syncPending" role="status" class="sync-notice">
      后台结果有更新；当前操作或未保存修改结束后将自动同步。也可使用面板的读取按钮重新加载。
    </p>
    <header class="core-heading">
      <div class="core-title">
        <span class="core-icon"><Database :size="20" /></span>
        <div>
          <span class="eyebrow">STEP 02 · ABA 周报</span>
          <h2>确定核心大词</h2>
        </div>
      </div>
      <span class="state" :class="{ complete: saved }">{{
        saved ? '已保存核心大词' : busy || '待筛选确认'
      }}</span>
    </header>
    <p class="intro">
      从已确认的商品词拓展搜索词，按 ABA
      搜索频率排名筛选。排名越小越靠前，最终商品词仍保留在第一步。
    </p>
    <p v-if="marketplace !== 'US'" class="notice">
      目前 ABA 自动获取仅支持美国站，请切换到美国站后使用。
    </p>
    <div class="controls">
      <div class="control-fields">
        <label
          ><span>ABA 周报截止日期</span
          ><input
            v-model="week"
            aria-label="ABA 周报截止日期"
            type="date"
            :max="latestReportWeek()"
            :disabled="!!busy"
          /><small>请选择已结束周的周六</small></label
        >
        <label class="brands"
          ><span>自家品牌 <em>选填</em></span
          ><input
            v-model="brandInput"
            aria-label="自家品牌（选填）"
            placeholder="例如 Acme，多个品牌用逗号分隔"
            :disabled="!!busy"
          /><small>品牌分类仅供参考，品牌词无需确认、可不填写</small></label
        >
      </div>
      <div class="control-actions">
        <button
          class="primary"
          :disabled="!!busy || marketplace !== 'US' || !week"
          @click="collect"
        >
          获取 ABA 周报
        </button>
        <button class="secondary" :disabled="!!busy" @click="refresh">
          <RefreshCw :size="14" />读取历史状态
        </button>
      </div>
    </div>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="busy" role="status" class="notice">
      {{ busy }}… 请等待；刷新页面后可通过历史状态恢复。
    </p>
    <section v-if="collectionJob" class="collection-board" aria-label="商品词采集与表格同步">
      <header>
        <div>
          <span class="section-kicker">采集进度</span>
          <h3>商品词采集与表格同步</h3>
          <small> 候选采集决定是否可继续；表格同步并行执行，失败不阻塞核心大词分析。 </small>
        </div>
        <a
          class="workbook-link"
          :href="coreApi.hotCollectionWorkbookUrl(run, collectionJob.id)"
          download
          >下载接口数据汇总 Excel</a
        >
      </header>
      <div class="collection-rules">
        <span>ABA 排名 ≤ 250,000</span>
        <span>四维综合分 ≥ 40</span>
        <span>词级相关度 ≥ 50%</span>
      </div>
      <div class="collection-list">
        <article v-for="task in collectionJob.tasks" :key="task.product_term">
          <div class="collection-term">
            <strong>{{ task.product_term }}</strong>
            <small>{{ collectionJob.week_start }} — {{ collectionJob.week_end }}</small>
          </div>
          <div>
            <span class="tag" :class="{ good: task.status === 'completed' }">{{
              collectionStatusLabels[task.status]
            }}</span>
            <small>第 {{ task.page }} 页 · {{ task.candidate_count }} 条</small>
          </div>
          <div>
            <span class="tag" :class="{ good: task.export_status === 'completed' }">{{
              exportStatusLabels[task.export_status]
            }}</span>
            <small>{{ task.last_output }}</small>
          </div>
          <div class="collection-error">
            <small v-if="task.error || task.export_error" class="error-detail">
              {{ task.error || task.export_error }}
            </small>
            <small v-else>最近更新：{{ new Date(task.updated_at).toLocaleString() }}</small>
            <button
              v-if="task.status === 'failed'"
              class="secondary"
              :disabled="!!busy"
              @click="retryCollectionTerm(task.product_term)"
            >
              重试该商品词
            </button>
          </div>
        </article>
      </div>
    </section>
    <template v-if="report">
      <div class="report-summary">
        <label class="summary-card summary-report"
          ><span class="summary-label">周报记录</span
          ><select
            :value="report.id"
            :disabled="!!busy"
            aria-label="周报记录"
            @change="changeReport"
          >
            <option v-if="!reports.some((x) => x.id === report?.id)" :value="report.id">
              {{ report.week_start }} — {{ report.week_end }}
            </option>
            <option v-for="r in reports" :key="r.id" :value="r.id">
              {{ r.week_start }} — {{ r.week_end }}{{ r.stale ? '（已失效）' : '' }}
            </option>
          </select></label
        >
        <div class="summary-card">
          <span class="summary-label">本次商品词</span>
          <strong>{{ report.roots_snapshot.join(' / ') }}</strong>
        </div>
        <div class="summary-card summary-count">
          <span class="summary-label">候选搜索词</span>
          <strong>{{ total.toLocaleString() }} <small>个</small></strong>
        </div>
      </div>
      <p v-if="report.stale || analysis?.stale || selection?.stale" class="notice">
        商品或商品词已变化，历史结果已失效，请重新获取周报并分析。
      </p>
      <p v-if="partial" class="notice">
        本周报为部分数据（每个商品词最多获取 1,000 行），当前排名筛选仅覆盖已获取范围。
      </p>
      <p v-if="manualFallback" class="notice manual-fallback" role="status">
        自动筛选没有合格核心词。以下均为 ABA 排名 25
        万以内候选，请结合排名、相关度、意图和品牌证据人工勾选；保存后自动流程会继续。
      </p>
      <p
        v-for="warning in [
          ...report.warnings,
          ...(analysis && !isHotAnalysis(analysis) ? analysis.warnings : []),
        ]"
        :key="warning"
        class="notice"
      >
        {{ warning }}
      </p>
      <div class="analysis-bar">
        <div>
          <span class="section-kicker">候选审核</span>
          <h3>搜索词候选</h3>
          <span>{{
            hotAnalysis ||
            ['hot-search-core-v1', 'hot-search-core-v2'].includes(report.rule_version ?? '')
              ? '审核全部搜索词的逐商品证据，再确认核心词。分页仅改变显示范围。'
              : '先分析当前页，再从推荐结果中确认核心词。'
          }}</span>
        </div>
        <button class="primary" :disabled="!canAnalyze" @click="analyze">
          {{
            ['hot-search-core-v1', 'hot-search-core-v2'].includes(report.rule_version ?? '')
              ? '分析全部搜索词'
              : '分析本页候选'
          }}
        </button>
        <button
          v-if="hotAnalysis?.status === 'queued'"
          class="secondary"
          :disabled="!!busy"
          @click="executeQueued"
        >
          继续已排队分析
        </button>
      </div>
      <div class="analysis-guide" aria-label="候选分析规则">
        <span
          ><b>01</b
          ><span><strong>逐词审核</strong><small>分页支持每页 5 / 10 / 20 条</small></span></span
        >
        <span
          ><b>02</b
          ><span><strong>证据核对</strong><small>核对相关度、精准意图与品牌</small></span></span
        >
        <span
          ><b>03</b
          ><span
            ><strong>确认核心词</strong><small>保存所有分页的选择，无数量截断</small></span
          ></span
        >
      </div>
      <details class="business-history">
        <summary>历史快照与版本选择</summary>
        <label v-if="analyses.length" class="history-label"
          >分析历史<select
            :value="analysis?.id || ''"
            :disabled="!!busy"
            aria-label="分析历史"
            @change="chooseAnalysis"
          >
            <option value="" disabled>选择已保存的分析</option>
            <option v-for="a in analyses" :key="a.id" :value="a.id">
              {{
                a.source_analysis_ids?.length
                  ? `汇总 ${a.source_analysis_ids.length} 批`
                  : `第 ${Math.floor(a.request.offset / a.request.limit) + 1} 页 · ${a.request.limit} 条/页`
              }}
              · {{ a.request.own_brands.join(', ') || '未声明品牌' }} · {{ a.status }} ·
              {{ a.id.slice(0, 8) }}
            </option>
          </select></label
        >
      </details>
      <p v-if="statusText" role="status" class="notice">
        {{ statusText }} {{ analysis && !isHotAnalysis(analysis) ? analysis.error_code : '' }}
      </p>
      <p
        v-if="hotAnalysis && ['queued', 'running'].includes(hotAnalysis.status)"
        role="status"
        class="notice"
      >
        逐词进度：{{ progress }} /
        {{ hotAnalysis.items.length }} 已处理；使用“读取历史状态”查看最新结果。
      </p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="pick">选择</th>
              <th>{{ hotReport ? '搜索词 / 来源商品词 / 报告周' : '搜索词 / 中文' }}</th>
              <th>ABA 排名</th>
              <template v-if="hotReport">
                <th>自然取得 / 有效样本 / 高相关数</th>
                <th>相关度 / 可信度</th>
                <th>四项精准意图证据</th>
                <th>品牌 / 入选或排除原因</th>
                <th>逐商品证据</th>
              </template>
              <template v-else
                ><th>筛选结论</th>
                <th>判断依据</th></template
              >
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in visibleRows" :key="row.id" :class="{ recommended: eligible(row) }">
              <td>
                <input
                  v-model="selected"
                  type="checkbox"
                  :value="row.id"
                  :aria-label="`选择 ${row.keyword}`"
                  :disabled="!!busy || !analysisValid || !selectable(row)"
                />
              </td>
              <td>
                <strong>{{ row.keyword }}</strong
                ><template v-if="hot(row)"
                  ><small>来源商品词：{{ hot(row)!.source_product_terms.join(' / ') }}</small
                  ><small>报告周：{{ report.week_start }} — {{ report.week_end }}</small></template
                >
                <small v-else>{{ term(row)?.translation || '待模型分析' }}</small>
              </td>
              <td class="rank">{{ row.search_frequency_rank.toLocaleString() }}</td>
              <template v-if="hot(row)">
                <td>
                  <small
                    >自然取得
                    {{ hotAnalysis?.collections[row.id]?.raw_natural_count ?? '—' }}</small
                  ><small>有效样本 {{ hot(row)!.summary?.valid_samples ?? '—' }}</small
                  ><small>高相关数 {{ hot(row)!.summary?.high_related_count ?? '—' }}</small>
                </td>
                <td>
                  <strong>{{ percent(hot(row)!.summary?.relevance_percent) }}</strong
                  ><small>{{
                    hot(row)!.summary ? confidenceLabels[hot(row)!.summary!.confidence] : '待评估'
                  }}</small
                  ><small>{{ itemStatus[hot(row)!.status] }}</small>
                </td>
                <td class="intent-evidence">
                  <span v-if="hot(row)!.intent_evidence" class="tag">{{
                    labels[hot(row)!.intent_evidence!.category]
                  }}</span
                  ><template v-for="gate in intentGates" :key="gate.key"
                    ><small
                      ><b
                        >{{ gate.label }}：{{
                          hot(row)!.intent_evidence
                            ? hot(row)!.intent_evidence![gate.key]
                              ? '通过'
                              : '不通过'
                            : '待判断'
                        }}</b
                      ></small
                    ><small>{{
                      hot(row)!.intent_evidence?.reasons[gate.key] || '暂无证据'
                    }}</small></template
                  >
                </td>
                <td class="reason">
                  <span class="tag">{{ labels[hot(row)!.brand_category] }}</span
                  ><small v-if="eligible(row)"
                    >入选：ABA 排名 ≤ 250,000，{{
                      v2Rules ? '相关度 ≥ 50%' : '相关度 > 60%'
                    }}，精准意图，通用词</small
                  ><small v-for="reason in exclusionReasons(row)" :key="reason">{{ reason }}</small>
                </td>
                <td>
                  <details class="product-evidence">
                    <summary>展开逐商品证据（{{ hot(row)!.comparisons.length }}）</summary>
                    <article
                      v-for="comparison in hot(row)!.comparisons"
                      :key="comparison.asin"
                      class="comparison"
                    >
                      <strong>{{ comparison.asin }}</strong
                      ><small
                        >自然位置 {{ comparison.natural_position ?? '—' }} · 综合
                        {{ comparison.overall_score ?? '—' }}</small
                      ><small
                        >用途 {{ comparison.purpose_score ?? '—' }}：{{
                          comparison.purpose_reason
                        }}</small
                      ><small
                        >款式结构 {{ comparison.style_structure_score ?? '—' }}：{{
                          comparison.style_structure_reason
                        }}</small
                      ><small
                        >规格 {{ comparison.specifications_score ?? '—' }}：{{
                          comparison.specifications_reason
                        }}</small
                      ><small
                        >价格 {{ comparison.price_score ?? '—' }}：{{
                          comparison.price_reason
                        }}</small
                      ><small v-if="comparison.exclusion_reason"
                        >剔除原因：{{ comparison.exclusion_reason }}</small
                      >
                    </article>
                    <p v-if="!hot(row)!.comparisons.length">暂无逐商品证据</p>
                  </details>
                </td>
              </template>
              <template v-else
                ><td>
                  <span v-if="row.root_reference" class="tag">商品词参考</span
                  ><template v-if="term(row)"
                    ><span class="tag" :class="{ good: term(row)?.recommended }">{{
                      labels[term(row)!.category] || term(row)?.category
                    }}</span
                    ><small
                      >{{
                        term(row)?.keyword_role === 'long_tail' ? '属性长尾词' : '核心品类候选'
                      }}
                      · {{ labels[term(row)!.relevance] }} · {{ labels[term(row)!.intent] }}</small
                    ></template
                  ><span v-else class="muted">待分析</span>
                </td>
                <td class="reason">
                  {{ term(row)?.reason || `匹配商品词：${row.matched_roots.join(' / ')}` }}
                </td></template
              >
            </tr>
            <tr v-if="!visibleRows.length">
              <td :colspan="hotReport ? 8 : 5" class="empty">
                {{ busy ? '正在读取候选…' : '没有匹配商品词的搜索词，请检查周报日期或商品词。' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pagination">
        <div class="page-summary">
          <label
            >每页显示
            <select
              v-model.number="pageSize"
              aria-label="每页显示数量"
              :disabled="!!busy"
              @change.stop="changePageSize"
            >
              <option :value="5">5 条</option>
              <option :value="10">10 条</option>
              <option :value="20">20 条</option>
            </select>
          </label>
          <span
            >第 {{ displayTotal ? paginationOffset + 1 : 0 }}–{{
              Math.min(paginationOffset + pageSize, displayTotal)
            }}
            项，共 {{ displayTotal }} 项</span
          >
        </div>
        <div class="page-actions">
          <span>第 {{ currentPage }} / {{ pageCount }} 页</span>
          <button :disabled="!!busy || paginationOffset === 0" @click="pageBy(-1)">上一页</button
          ><button
            :disabled="!!busy || paginationOffset + pageSize >= displayTotal"
            @click="pageBy(1)"
          >
            下一页
          </button>
        </div>
      </div>
      <footer class="core-footer">
        <span>{{
          saved
            ? `已确认保存 ${selection?.items.length} 个核心词`
            : `已选 ${selected.length} 个通用核心词，请核对后确认`
        }}</span
        ><button class="primary" :disabled="!canSave" @click="save">
          <Check :size="15" />{{ saved ? '已确认保存' : '确认核心大词' }}
        </button>
      </footer>
    </template>
    <div v-else-if="!busy && !error" class="empty-state">
      <Database :size="28" /><strong>从一份 ABA 周报开始</strong
      ><span>选择报告日期，获取已确认商品词对应的搜索词与真实 ABA 排名。</span>
    </div>
    <p class="footnote">
      周报来自卖家后台登录会话；失效时需要重新登录。分析由工作流或分析按钮创建，刷新与读取历史仅恢复已保存结果。
    </p>
    <SourceExportsPanel :run="run" :kinds="['step2']" />
  </section>
</template>

<style scoped>
.core-panel {
  min-width: 0;
  max-width: 100%;
  background: white;
  border: 1px solid var(--line);
  border-radius: 16px;
  margin: 24px 0;
  padding: 26px;
  scroll-margin-top: 24px;
  color: var(--ink, #273833);
}
.core-heading,
.core-title,
.analysis-bar,
.core-footer,
.pagination {
  display: flex;
  align-items: center;
  gap: 16px;
}
.core-heading,
.analysis-bar,
.core-footer,
.pagination {
  justify-content: space-between;
}
.core-title {
  gap: 12px;
}
.core-icon {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: var(--teal-soft);
  color: var(--teal);
}
.eyebrow {
  font-size: 10px;
  letter-spacing: 1.5px;
  color: var(--teal);
  font-weight: 700;
}
h2 {
  font-size: 20px;
  margin: 4px 0 0;
}
h3 {
  font-size: 15px;
  margin: 0 0 6px;
}
.intro,
.footnote {
  font-size: 12px;
  line-height: 1.8;
  color: #718078;
}
.intro {
  margin: 14px 0 22px;
}
.state {
  font-size: 12px;
  background: #f3f5f3;
  padding: 7px 12px;
  border-radius: 20px;
  color: #68756e;
}
.state.complete {
  background: var(--teal-soft);
  color: var(--teal);
}
.controls {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  gap: 20px;
  padding: 18px;
  background: #f8faf8;
  border: 1px solid var(--line);
  border-radius: 12px;
}
.control-fields {
  display: grid;
  grid-template-columns: minmax(190px, 0.7fr) minmax(280px, 1.3fr);
  gap: 16px;
}
.controls label {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
}
.controls label > span {
  display: flex;
  align-items: center;
  gap: 7px;
}
.controls label em {
  padding: 2px 6px;
  border-radius: 4px;
  background: #eaf0ec;
  color: #78867d;
  font-size: 9px;
  font-style: normal;
  font-weight: 600;
}
.brands {
  min-width: 240px;
}
.control-actions {
  display: grid;
  grid-template-columns: 1fr;
  gap: 8px;
  min-width: 142px;
}
small {
  display: block;
  font-size: 11px;
  color: #839088;
  font-weight: 400;
  line-height: 1.6;
}
input:not([type='checkbox']),
select {
  border: 1px solid #dce4de;
  border-radius: 7px;
  padding: 10px;
  background: white;
  color: #324039;
  font: inherit;
  min-width: 0;
}
input[type='checkbox'] {
  accent-color: var(--teal);
  width: 16px;
  height: 16px;
}
button {
  border: 1px solid #dce4de;
  border-radius: 7px;
  padding: 10px 14px;
  background: white;
  font: inherit;
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  cursor: pointer;
  white-space: nowrap;
}
.primary {
  background: var(--teal);
  border-color: var(--teal);
  color: white;
}
button:disabled {
  opacity: 0.45;
  cursor: default;
}
.collection-board {
  margin-top: 20px;
  padding: 16px;
  border: 1px solid #dbe7e0;
  border-radius: 12px;
  background: #fbfdfc;
}
.collection-board > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.workbook-link {
  flex: 0 0 auto;
  padding: 9px 12px;
  border: 1px solid var(--teal);
  border-radius: 7px;
  color: var(--teal);
  font-size: 12px;
  font-weight: 600;
  text-decoration: none;
}
.collection-rules {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 14px 0;
}
.collection-rules span {
  padding: 5px 8px;
  border-radius: 6px;
  background: var(--teal-soft);
  color: var(--teal);
  font-size: 11px;
  font-weight: 600;
}
.collection-list {
  display: grid;
  gap: 8px;
}
.collection-list article {
  display: grid;
  grid-template-columns: minmax(150px, 1.1fr) minmax(120px, 0.7fr) minmax(190px, 1.3fr) minmax(
      180px,
      1fr
    );
  align-items: center;
  gap: 12px;
  padding: 11px 12px;
  border: 1px solid #e3e9e5;
  border-radius: 9px;
  background: white;
}
.collection-term,
.collection-error {
  min-width: 0;
}
.collection-term strong,
.collection-term small,
.collection-list small {
  overflow-wrap: anywhere;
}
.collection-error button {
  margin-top: 6px;
}
.error-detail {
  color: #a44343;
}
.report-summary {
  display: grid;
  grid-template-columns: minmax(260px, 1.35fr) minmax(180px, 1fr) minmax(120px, 0.55fr);
  gap: 12px;
  margin-top: 20px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--line);
}
.summary-card {
  display: flex;
  min-width: 0;
  flex-direction: column;
  justify-content: center;
  gap: 8px;
  min-height: 70px;
  padding: 12px 14px;
  border: 1px solid #e3e9e5;
  border-radius: 10px;
  background: #fbfcfb;
}
.summary-card strong {
  overflow: hidden;
  color: #34463d;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.summary-label {
  color: #859189;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
}
.summary-report select {
  width: 100%;
  padding: 8px 10px;
}
.summary-count strong {
  color: var(--teal);
  font-size: 20px;
  font-variant-numeric: tabular-nums;
}
.summary-count small {
  display: inline;
  color: #718078;
  font-size: 11px;
}
.analysis-bar {
  align-items: flex-end;
  margin: 24px 0 12px;
}
.analysis-bar span,
.history-label,
.pagination,
.core-footer {
  font-size: 12px;
  color: #718078;
}
.analysis-bar > div > span:last-child {
  display: block;
  line-height: 1.6;
}
.section-kicker {
  display: block;
  margin-bottom: 4px;
  color: var(--teal) !important;
  font-size: 10px !important;
  font-weight: 700;
  letter-spacing: 0.08em;
}
.analysis-guide {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}
.analysis-guide > span {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 11px 12px;
  border-radius: 9px;
  background: #f6f9f7;
}
.analysis-guide b {
  flex: 0 0 auto;
  color: #a1ada5;
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}
.analysis-guide > span > span {
  min-width: 0;
}
.analysis-guide strong {
  display: block;
  margin-bottom: 2px;
  color: #45574d;
  font-size: 11px;
}
.analysis-guide small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.history-label {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}
.table-wrap {
  max-width: 100%;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 10px;
}
.intent-evidence {
  min-width: 190px;
}
.product-evidence {
  min-width: 210px;
  max-width: 300px;
  overflow-wrap: anywhere;
}
.comparison {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--line);
}
table {
  border-collapse: collapse;
  width: 100%;
  min-width: 760px;
  text-align: left;
  font-size: 12px;
}
th {
  background: #f6f8f6;
  padding: 12px 14px;
  color: #79857d;
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
}
td {
  padding: 15px 14px;
  border-top: 1px solid var(--line);
  vertical-align: top;
}
td strong {
  font-size: 13px;
  font-weight: 600;
}
.pick {
  width: 44px;
}
.rank {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.reason {
  max-width: 280px;
  line-height: 1.7;
  color: #718078;
}
.tag {
  display: inline-block;
  border-radius: 4px;
  padding: 3px 7px;
  background: #f0f2f0;
  color: #6c7970;
  font-size: 10px;
  margin: 0 4px 4px 0;
}
.tag.good {
  background: var(--teal-soft);
  color: var(--teal);
}
tr.recommended {
  background: #f5fbf8;
}
.pagination {
  margin: 14px 0 24px;
}
.page-summary,
.page-summary label,
.page-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.page-summary {
  flex-wrap: wrap;
}
.page-summary label {
  color: #718078;
}
.page-summary select {
  width: auto;
  padding: 7px 26px 7px 9px;
}
.page-actions {
  flex: 0 0 auto;
}
.page-actions > span {
  margin-right: 4px;
  color: #52665b;
  font-variant-numeric: tabular-nums;
}
.core-footer {
  padding-top: 20px;
  border-top: 1px solid var(--line);
}
.notice,
.error {
  font-size: 12px;
  line-height: 1.7;
  padding: 12px 14px;
  border-radius: 8px;
  margin: 14px 0;
  background: #fff7e8;
  color: #8b682b;
}
.error {
  background: #fff0ee;
  color: #b44b43;
}
.empty,
.empty-state {
  text-align: center;
  color: #839088;
  padding: 35px;
}
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  margin: 25px 0;
}
.empty-state strong {
  font-size: 15px;
  color: #43594c;
}
.muted {
  color: #96a198;
}
.footnote {
  margin: 18px 0 0;
  font-size: 11px;
}
@media (max-width: 700px) {
  .core-panel {
    padding: 18px 14px;
  }
  .core-heading {
    align-items: flex-start;
  }
  .state {
    max-width: 110px;
    text-align: center;
  }
  .core-title h2 {
    font-size: 18px;
  }
  .controls {
    padding: 14px;
    gap: 14px;
    grid-template-columns: 1fr;
  }
  .control-fields {
    grid-template-columns: 1fr;
  }
  .control-actions {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    min-width: 0;
  }
  .brands {
    min-width: 0;
  }
  .collection-board > header {
    align-items: flex-start;
    flex-direction: column;
  }
  .collection-list article {
    grid-template-columns: 1fr;
  }
  .report-summary {
    grid-template-columns: 1fr 1fr;
  }
  .summary-report {
    grid-column: 1 / -1;
  }
  .summary-report select,
  .history-label select {
    max-width: 100%;
  }
  .analysis-bar {
    align-items: flex-start;
    gap: 12px;
  }
  .analysis-bar span {
    line-height: 1.7;
    display: block;
  }
  .analysis-guide {
    grid-template-columns: 1fr;
  }
  .analysis-guide small {
    white-space: normal;
  }
  .history-label {
    align-items: flex-start;
    flex-direction: column;
  }
  .core-footer {
    align-items: flex-start;
    gap: 14px;
  }
  .core-footer span {
    line-height: 1.8;
  }
  .pagination {
    align-items: stretch;
    flex-direction: column;
    gap: 8px;
  }
  .page-summary,
  .page-actions {
    justify-content: space-between;
  }
  .pagination button {
    padding: 8px 10px;
  }
}
</style>

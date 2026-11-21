<script setup lang="ts">
import SourceExportsPanel from './SourceExportsPanel.vue';
import { usePanelSync } from '../panelSync';
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import {
  libraryApi,
  type LibraryEntry,
  type LibraryReport,
  type LibraryTemplate,
  type LibraryRelevanceJob,
} from '../api/library';
import { trafficApi, type TrafficReport } from '../api/traffic';
import type { InputRun } from '../api/images';

const props = defineProps<{ run: InputRun; ready: boolean; sourceRevision: number }>();
const emit = defineEmits<{
  saved: [];
  status: [value: 'waiting' | 'running' | 'review' | 'done'];
}>();
const history = ref<LibraryReport[]>([]);
const template = ref<LibraryTemplate | null>(null);
const sources = ref<TrafficReport[]>([]);
const sourceId = ref('');
const selectedId = ref('');
const report = computed(() => history.value.find((r) => r.id === selectedId.value));
const keyword = ref('');
const entry = computed(() => report.value?.entries.find((e) => e.keyword === keyword.value));
const tier = ref('all');
// 仅控制展示/导出范围，不会删除快照中的未排名词。
const rankedOnly = ref(true);
const relevanceJob = ref<LibraryRelevanceJob | null>(null);
const relevanceError = ref('');
let relevanceTimer: ReturnType<typeof setTimeout> | undefined;
let relevanceTicket = 0;
function scheduleRelevancePoll(ticket: number) {
  if (!alive || ticket !== relevanceTicket || !relevanceJob.value || relevanceJob.value.status === 'completed') return;
  relevanceTimer = setTimeout(async () => {
    const job = relevanceJob.value;
    const library = report.value;
    if (!job || !library || ticket !== relevanceTicket) return;
    try {
      const updated = await libraryApi.relevanceGet(props.run, library.id, job.id);
      if (!alive || ticket !== relevanceTicket) return;
      relevanceJob.value = updated;
      relevanceError.value = '';
      scheduleRelevancePoll(ticket);
    } catch (e) {
      if (!alive || ticket !== relevanceTicket) return;
      relevanceError.value = e instanceof Error ? e.message : '核验进度读取失败';
      scheduleRelevancePoll(ticket);
    }
  }, 3000);
}
watch([() => props.run.id, () => report.value?.id, () => report.value?.version, rankedOnly], async () => {
  const ticket = ++relevanceTicket;
  clearTimeout(relevanceTimer);
  relevanceJob.value = null;
  relevanceError.value = '';
  const library = report.value;
  if (!library) return;
  try {
    let job = await libraryApi.relevanceLatest(props.run, library.id, rankedOnly.value);
    if (!alive || ticket !== relevanceTicket) return;
    if (!job) job = await libraryApi.relevanceStart(props.run, library.id, rankedOnly.value);
    if (!alive || ticket !== relevanceTicket) return;
    relevanceJob.value = job?.library_version === library.version ? job : null;
    scheduleRelevancePoll(ticket);
  } catch (e) {
    if (alive && ticket === relevanceTicket)
      relevanceError.value = e instanceof Error ? e.message : '核验任务读取失败';
  }
});
const rankedCount = computed(
  () => report.value?.entries.filter((e) => (e.evidence.metrics.rank ?? 0) > 0).length ?? 0,
);
const search = ref('');
const offset = ref(0);
const filtered = computed(
  () =>
    report.value?.entries.filter(
      (e) =>
        (!rankedOnly.value || (e.evidence.metrics.rank ?? 0) > 0) &&
        (tier.value === 'all' || e.tier === tier.value) &&
        e.keyword.includes(search.value.trim().toLowerCase()),
    ) ?? [],
);
const templateRows = computed(
  () => new Map(template.value?.rows.map((row) => [String(row[0]), row]) ?? []),
);
// 展示与 Excel 共用后端模板，切换历史版本时拒绝旧请求回填。
let templateTicket = 0;
watch(report, async (value) => {
  const ticket = ++templateTicket;
  template.value = null;
  if (!value) return;
  try {
    const data = await libraryApi.template(props.run, value.id);
    if (ticket === templateTicket && alive) template.value = data;
  } catch (e) {
    if (ticket === templateTicket && alive)
      error.value = e instanceof Error ? e.message : '模板加载失败';
  }
});
const page = computed(() => filtered.value.slice(offset.value, offset.value + 20));
const busy = ref(false);
// Read-only restoration must not change saved execution status.
const reading = ref(true);
const dirty = ref(false);
const error = ref('');
const priority = ref<LibraryEntry['priority']>('normal');
const usage = ref<LibraryEntry['usage']>('pending');
const asins = ref('');
const operator = ref('');
const reason = ref('');
const confirmed = ref(false);
const publishReason = ref('');
const publisher = ref('');
const publicationDirty = ref(false);
const publicationVersion = ref(0);
const editVersion = ref(0);
const locked = computed(
  () =>
    !props.ready ||
    !report.value ||
    report.value.stale ||
    report.value.publication_version !== null,
);
const labels: Record<string, string> = {
  unranked: '未分层',
  high: '高',
  normal: '普通',
  low: '低',
  pending: '待使用',
  listing: 'Listing',
  advertising: '广告',
  both: 'Listing 与广告',
  paused: '暂停',
  unknown: '未知',
};
const label = (value: string) => labels[value] ?? value;
const tierLabel = (value: string) => (value === 'unranked' ? '未分层' : value);
let epoch = 0;
let alive = true;
onBeforeUnmount(() => {
  alive = false;
  relevanceTicket++;
  clearTimeout(relevanceTimer);
  epoch++;
});
const current = (token: number) => alive && token === epoch;
function restore() {
  priority.value = entry.value?.priority ?? 'normal';
  usage.value = entry.value?.usage ?? 'pending';
  asins.value = entry.value?.auxiliary_asins.join(', ') ?? '';
  reason.value = '';
  editVersion.value = report.value?.version ?? 0;
  dirty.value = false;
  confirmed.value = false;
  publicationVersion.value = report.value?.version ?? 0;
  publicationDirty.value = false;
}
function accept(value: LibraryReport) {
  offset.value = 0;
  tier.value = 'all';
  search.value = '';
  history.value = [value, ...history.value.filter((r) => r.id !== value.id)];
  selectedId.value = value.id;
  if (!value.entries.some((e) => e.keyword === keyword.value))
    keyword.value = value.entries[0]?.keyword ?? '';
  restore();
}
async function refresh(discard = true) {
  reading.value = true;
  const token = ++epoch;
  busy.value = true;
  error.value = '';
  try {
    const [hs, ts] = await Promise.all([
      libraryApi.history(props.run),
      trafficApi.history(props.run),
    ]);
    if (!current(token)) return;
    history.value = hs;
    sources.value = ts.filter((t) => t.complete && !t.stale);
    if (!sources.value.some((t) => t.id === sourceId.value))
      sourceId.value = sources.value[0]?.id ?? '';
    if (!hs.some((r) => r.id === selectedId.value)) selectedId.value = hs[0]?.id ?? '';
    if (discard || (!dirty.value && !publicationDirty.value)) {
      if (!report.value?.entries.some((e) => e.keyword === keyword.value))
        keyword.value = report.value?.entries[0]?.keyword ?? '';
      restore();
    }
  } catch (e) {
    if (current(token)) error.value = e instanceof Error ? e.message : '读取失败';
  } finally {
    if (current(token)) { busy.value = false; reading.value = false; }
  }
}
async function act(operation: () => Promise<LibraryReport>) {
  const token = ++epoch;
  busy.value = true;
  error.value = '';
  try {
    const value = await operation();
    if (current(token)) {
      accept(value);
      emit('saved');
    }
  } catch (e) {
    if (current(token)) error.value = e instanceof Error ? e.message : '操作失败';
  } finally {
    if (current(token)) busy.value = false;
  }
}
async function generate() {
  const source = sources.value.find((t) => t.id === sourceId.value);
  if (source) await act(() => libraryApi.create(props.run, source.id, source.version));
}
async function refreshAttachments() {
  const r = report.value;
  if (r) await act(() => libraryApi.refreshAttachments(props.run, r));
}
async function revise() {
  const r = report.value;
  if (r) await act(() => libraryApi.revise(props.run, r));
}
async function save() {
  if (!report.value || !entry.value) return;
  const r = report.value;
  const data = {
    expected_version: editVersion.value,
    keyword: entry.value.keyword,
    priority: priority.value,
    usage: usage.value,
    auxiliary_asins: asins.value.split(/[\s,，;；]+/).filter(Boolean),
    operator: operator.value.trim(),
    reason: reason.value.trim(),
  };
  await act(() => libraryApi.edit(props.run, r.id, data));
}
async function publish() {
  if (!report.value || !confirmed.value || dirty.value) return;
  const r = report.value;
  await act(() =>
    libraryApi.publish(props.run, r.id, {
      expected_version: publicationVersion.value,
      publisher: publisher.value.trim(),
      reason: publishReason.value.trim(),
      confirmed: true,
    }),
  );
}
async function download() {
  if (!report.value) return;
  const token = ++epoch;
  busy.value = true;
  error.value = '';
  try {
    await libraryApi.download(props.run, report.value.id, rankedOnly.value);
  } catch (e) {
    if (current(token)) error.value = e instanceof Error ? e.message : '下载失败';
  } finally {
    if (current(token)) busy.value = false;
  }
}
function selectHistory() {
  keyword.value = report.value?.entries[0]?.keyword ?? '';
  offset.value = 0;
  restore();
}
function choose(value: LibraryEntry) {
  keyword.value = value.keyword;
  restore();
}
watch([tier, search, rankedOnly], () => {
  offset.value = 0;
});
const syncPending = usePanelSync(
  () => [props.sourceRevision, props.ready],
  () => busy.value || dirty.value || publicationDirty.value,
  async () => {
    await refresh(false);
  },
);
watch([report, reading, () => props.ready, busy], () => {
    if (reading.value) return;
  const freshPublished = history.value.some((r) => r.publication_version !== null && !r.stale);
  emit(
    'status',
    props.ready && freshPublished
      ? 'done'
      : busy.value
        ? 'running'
        : report.value && !report.value.stale
          ? 'review'
          : 'waiting',
  );
});
void refresh();
</script>

<template>
  <section id="library" class="library-panel" aria-label="层级填充与发布">
    <p v-if="syncPending" role="status" class="sync-notice">
      后台结果有更新；当前操作或未保存修改结束后将自动同步。也可使用面板的读取按钮重新加载。
    </p>
    <div class="library-heading">
      <div>
        <p class="kicker">SOP 08 · 关键词库</p>
        <h2>层级填充与发布</h2>
      </div>
      <span>真实审核结果 · 版本留档</span>
    </div>
    <p>
      按所选周 ABA 排名分层：L1 ≤ 30,000，L2 ≤ 150,000，L3 ≤ 300,000，L4 &gt;
      300,000。无排名的人工保留词单列“未分层”。
    </p>
    <div class="library-controls">
      <label
        >已完成的流量筛选<select
          v-model="sourceId"
          aria-label="已完成的流量筛选"
          :disabled="busy || dirty || publicationDirty"
        >
          <option value="">请选择</option>
          <option v-for="s in sources" :key="s.id" :value="s.id">
            {{ s.created_at.slice(0, 19) }} · 审核 v{{ s.version }}
          </option>
        </select></label
      >
      <button
        type="button"
        :disabled="busy || dirty || publicationDirty || !ready || !sourceId"
        @click="generate"
      >
        生成入库草稿
      </button>
      <button type="button" :disabled="busy" @click="refresh(true)">
        读取入库历史（丢弃未保存修改）
      </button>
    </div>
    <p v-if="!ready">请先完成第七步审核；已有版本仍可查看和导出。</p>
    <p v-if="error" role="alert" class="library-error">{{ error }}</p>
    <p v-if="busy" role="status">{{ reading ? '正在读取已保存的结果…' : '正在保存…' }}</p>
    <template v-if="report">
      <details class="business-history">
        <summary>历史快照与版本选择</summary>
        <label
          >入库版本<select
            v-model="selectedId"
            aria-label="入库版本"
            :disabled="busy || dirty || publicationDirty"
            @change="selectHistory"
          >
            <option v-for="r in history" :key="r.id" :value="r.id">
              {{ r.publication_version === null ? '草稿' : `发布 v${r.publication_version}` }} ·
              {{ r.created_at.slice(0, 19) }}{{ r.stale ? ' · 来源已变更' : '' }}
            </option>
          </select></label
        >
      </details>
      <p v-if="report.stale" class="library-error">
        来源已变更：此版本仅供历史查看，请用最新审核结果生成草稿。
      </p>
      <p v-if="report.publication_version !== null" class="library-success">
        已发布 v{{ report.publication_version }} · {{ report.publication?.publisher }} ·
        {{ report.publication?.reason }}。该版本只读。
      </p>
      <button
        v-if="report.publication_version !== null"
        type="button"
        :disabled="busy || dirty || publicationDirty || report.stale || !ready"
        @click="revise"
      >
        基于此版本创建修订草稿
      </button>
      <p>
        共 {{ report.entries.length }} 词 · 草稿修订 {{ report.version }} ·
        {{ report.entries.filter((e) => e.tier === 'unranked').length }} 个未分层词
      </p>
      <div class="library-controls">
        <label
          >筛选层级<select
            v-model="tier"
            aria-label="筛选层级"
            :disabled="busy || dirty || publicationDirty"
          >
            <option value="all">全部层级</option>
            <option v-for="t in ['L1', 'L2', 'L3', 'L4', 'unranked']" :key="t" :value="t">
              {{ tierLabel(t) }}
            </option>
          </select></label
        ><label
          >搜索关键词<input
            v-model="search"
            aria-label="搜索关键词"
            :disabled="busy || dirty || publicationDirty" /></label
        ><button type="button" :disabled="busy" @click="download">下载关键词库 CSV</button>
      </div>
      <section v-if="template" class="template-summary" aria-label="关键词库模板汇总">
        <h3>关键词库</h3>
        <table>
          <tbody>
            <tr v-for="line in template.summary" :key="line[0]">
              <th>{{ line[0] }}</th>
              <td>{{ line.slice(1).filter(Boolean).join('、') }}</td>
            </tr>
          </tbody>
        </table>
      </section>
      <label><input v-model="rankedOnly" type="checkbox" />只显示及导出有 ABA 周排名的关键词</label>
      <p class="muted">
        有 ABA 排名 {{ rankedCount }} 词；无排名
        {{
          report.entries.length - rankedCount
        }}
        词。主表默认只展示有排名词，无排名词保留为来源候选。
      </p>
      <section aria-label="Excel相关性核验">
        <p role="status">{{ relevanceJob?.status === 'completed' ? '最终 Excel 已自动生成' : relevanceJob?.status === 'failed' && !relevanceJob.next_retry_at ? '核验未完成，请检查异常信息' : '相关性核验及最终 Excel 自动生成中，无需点击启动' }}</p>
        <p v-if="relevanceJob" class="muted">
          已完成 {{ relevanceJob.completed }} / {{ relevanceJob.total }} 个关键词；失败 {{ relevanceJob.failed }} 个。
          <span v-if="relevanceJob.current_keyword">当前{{ relevanceJob.phase === 'collecting' ? '采集' : '核验' }}：{{ relevanceJob.current_keyword }}</span>
          已处理词的有效样本缺口 {{ relevanceJob.missing_samples }} 个（每词目标 30 个）。
          <span v-if="relevanceJob.status === 'completed'">相关性核验完成，可以下载。</span>
        </p>
        <p v-if="relevanceError || relevanceJob?.error" role="alert">{{ relevanceError || relevanceJob?.error }}</p>
        <a
          v-if="relevanceJob?.status === 'completed'" class="template-download"
          :href="libraryApi.relevanceFile(run, report.id, relevanceJob.id)">下载关键词库及来源附表 Excel（相关性百分比）</a>
        <p class="muted">相关性 = 高相关商品数 ÷ 有效核验商品数；每词目标 30 个自然商品。核验及有限重试自动执行，完成后可重复下载。</p>
      </section>
      <button v-if="report.publication_version === null" type="button"
        :disabled="busy || dirty || publicationDirty || report.stale" @click="refreshAttachments">更新草稿附表</button>
      <p class="muted">
        以下按原模板列顺序展示；缺失指标留空。CTR、CVR、花费等投放数据等待后续接入。
      </p>
      <details v-if="template?.rule_notes?.length" class="business-history" open>
        <summary>购买性、相关性与垄断性判定依据（SOP）</summary>
        <p v-for="note in template.rule_notes" :key="note">{{ note }}</p>
        <p>
          表格右侧展示 ABA 计算依据、人工相关性证据与 SOP
          投放建议。历史版本按其规则快照展示；更新规则请从第七步生成新的流量筛选，再生成关键词库。
        </p>
      </details>
      <div class="library-table">
        <table>
          <thead>
            <tr>
              <th v-for="column in template?.headers ?? []" :key="column">{{ column }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="e in page" :key="e.keyword" :class="{ selected: e.keyword === keyword }">
              <td v-for="(value, index) in templateRows.get(e.keyword) ?? []" :key="index">
                <button
                  v-if="index === 0"
                  type="button"
                  :aria-label="`编辑 ${e.keyword}`"
                  :disabled="busy || dirty || publicationDirty"
                  @click="choose(e)"
                >
                  {{ value }}
                </button>
                <template v-else>{{ value }}</template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="library-controls">
        <button :disabled="busy || dirty || publicationDirty || offset === 0" @click="offset -= 20">
          上一页</button
        ><span>第 {{ Math.floor(offset / 20) + 1 }} 页 · 匹配 {{ filtered.length }} 词</span
        ><button
          :disabled="busy || dirty || publicationDirty || offset + 20 >= filtered.length"
          @click="offset += 20"
        >
          下一页
        </button>
      </div>
      <form v-if="entry" @submit.prevent="save">
        <h3>关键词：{{ entry.keyword }}</h3>
        <p>
          审核理由：{{ entry.evidence.review?.data.reason }}；证据：{{
            entry.evidence.review?.data.evidence_note
          }}
        </p>
        <fieldset
          :disabled="busy || locked || publicationDirty"
          @input="dirty = true"
          @change="dirty = true"
        >
          <legend>填充入库字段</legend>
          <div class="library-controls">
            <label
              >入库优先级<select v-model="priority" aria-label="入库优先级">
                <option value="high">高</option>
                <option value="normal">普通</option>
                <option value="low">低</option>
              </select></label
            >
            <label
              >使用状态<select v-model="usage" aria-label="使用状态">
                <option
                  v-for="u in ['pending', 'listing', 'advertising', 'both', 'paused']"
                  :key="u"
                  :value="u"
                >
                  {{ label(u) }}
                </option>
              </select></label
            >
            <label
              >辅助 ASIN（逗号分隔，最多 30 个）<input
                v-model="asins"
                aria-label="辅助 ASIN（逗号分隔，最多 30 个）"
            /></label>
            <label
              >操作人（本地记录）<input
                v-model="operator"
                aria-label="操作人（本地记录）"
                maxlength="200"
                required
            /></label>
            <label
              >修改说明<input v-model="reason" aria-label="修改说明" maxlength="200" required
            /></label>
            <button type="submit" :disabled="!dirty">保存入库字段</button>
          </div>
        </fieldset>
        <p v-if="dirty">有未保存修改，请保存或重新读取历史后继续。</p>
      </form>
      <details>
        <summary>各层辅助 ASIN 建议（最多 30 个）</summary>
        <p>
          统计已入库词的同周 ABA 前三 ASIN；同一词重复来源只计一次。建议需要人工填写到关键词后保存。
        </p>
        <p v-for="t in ['L1', 'L2', 'L3', 'L4']" :key="t">
          {{ t }}：{{
            report.suggestions
              .filter((s) => s.tier === t)
              .map((s) => `${s.asin}（${s.keyword_count} 词）`)
              .join('、') || '暂无证据'
          }}
        </p>
      </details>
      <details>
        <summary>来源、规则与修改记录</summary>
        <p>
          规则 {{ report.rules.id }} · L1 ≤ {{ report.rules.l1_max.toLocaleString() }}，L2 ≤
          {{ report.rules.l2_max.toLocaleString() }}，L3 ≤
          {{ report.rules.l3_max.toLocaleString() }}，其余 L4； 无有效排名单列未分层 · ASIN 取每词前
          {{ report.rules.asin_positions }} 位、每层最多 {{ report.rules.suggestions_per_tier }} 个
        </p>
        <p>来源摘要：{{ report.source_sha256 }}</p>
        <p>
          流量快照：{{ report.request.traffic_id }} · 审核 v{{ report.request.traffic_version }}
        </p>
        <p v-for="edit in report.edits" :key="edit.version">
          修订 {{ edit.version }} · {{ edit.data.operator }} · {{ edit.data.reason }}
        </p>
        <p>
          完整来源周期、审核证据和规则随 CSV 导出。CTR、CVR、花费、订单、ACOS 尚无数据，保持空值。
        </p>
      </details>
      <form v-if="report.publication_version === null" @submit.prevent="publish">
        <fieldset
          :disabled="busy || locked || dirty"
          @input="publicationDirty = true"
          @change="publicationDirty = true"
        >
          <legend>确认发布不可变版本</legend>
          <div class="library-controls">
            <label
              >发布人（本地记录）<input
                v-model="publisher"
                aria-label="发布人（本地记录）"
                required
                maxlength="200" /></label
            ><label
              >发布说明<input
                v-model="publishReason"
                aria-label="发布说明"
                required
                maxlength="200"
            /></label>
          </div>
          <label class="confirmation"
            ><input
              v-model="confirmed"
              type="checkbox"
            />我已核对关键词、未分层词和用途；后续调整将生成新版本。</label
          ><button type="submit" :disabled="!confirmed">
            发布关键词库版本
          </button>
        </fieldset>
      </form>
    </template>
    <p v-else>尚无入库版本。完成审核后生成草稿，核对后发布。</p>
    <SourceExportsPanel :run="run" />
  </section>
</template>

<style scoped>
.template-summary {
  margin: 18px 0;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  overflow: hidden;
}
.template-summary h3 {
  padding: 12px;
  margin: 0;
  background: #f0f7f5;
}
.template-summary table {
  width: 100%;
}
.template-summary th {
  width: 130px;
  white-space: nowrap;
}
.template-download {
  display: inline-block;
  margin: 12px 0;
  color: #047867;
}
.library-table td {
  min-width: 110px;
}
.library-table td:first-child {
  min-width: 240px;
}

.library-panel {
  padding: 28px;
  margin-top: 24px;
  background: white;
  border: 1px solid #e2e8f0;
  border-radius: 18px;
  color: #334155;
  min-width: 0;
}
.library-heading {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  align-items: center;
}
.library-heading h2 {
  margin: 6px 0;
  color: #0f172a;
  font-size: 24px;
}
.kicker {
  color: #14856f;
  font-size: 12px;
  letter-spacing: 2px;
}
.library-heading > span {
  font-size: 12px;
  color: #64748b;
}
.library-panel p {
  font-size: 13px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.library-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 12px;
  margin: 16px 0;
}
.library-panel label {
  display: flex;
  flex-direction: column;
  gap: 7px;
  font-size: 13px;
}
.library-panel input,
.library-panel select {
  padding: 9px 10px;
  border: 1px solid #cbd5e1;
  border-radius: 7px;
  font: inherit;
  max-width: 100%;
  min-width: 0;
}
.library-panel button {
  padding: 9px 12px;
  background: #0f766e;
  color: white;
  border: 0;
  border-radius: 7px;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.library-panel button:disabled {
  opacity: 0.45;
  cursor: default;
}
.library-panel fieldset {
  border: 1px solid #cbd5e1;
  border-radius: 10px;
  margin: 20px 0;
  padding: 16px;
  min-width: 0;
}
.library-table {
  overflow: auto;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
}
.library-table table {
  border-collapse: collapse;
  font-size: 12px;
  min-width: 1100px;
  width: 100%;
}
.library-table th,
.library-table td {
  padding: 12px;
  text-align: left;
  border-bottom: 1px solid #e2e8f0;
}
.library-table th {
  background: #f8fafc;
  white-space: nowrap;
}
.library-table button {
  background: transparent;
  color: #0f766e;
  text-align: left;
  padding: 0;
}
.selected {
  background: #f0fdfa;
}
.library-error {
  color: #b42318;
}
.library-success {
  color: #08745d;
}
.library-panel details {
  border-top: 1px solid #e2e8f0;
  padding: 16px 0;
  font-size: 13px;
}
.confirmation {
  flex-direction: row !important;
  align-items: center;
  margin-bottom: 16px;
}
@media (max-width: 600px) {
  .library-panel {
    padding: 16px;
  }
  .library-heading {
    align-items: start;
    flex-direction: column;
    gap: 4px;
  }
  .library-controls {
    align-items: stretch;
    flex-direction: column;
  }
  .library-panel label {
    width: 100%;
  }
  .library-panel select {
    width: 100%;
  }
  .library-heading h2 {
    font-size: 21px;
  }
}
</style>

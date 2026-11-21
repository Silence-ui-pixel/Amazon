<script setup lang="ts">
import { usePanelSync } from '../panelSync';
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import {
  cleaningApi,
  type Category,
  type CleaningReport,
  type CleaningPage,
  type ClassificationProgress,
  type RootReview,
} from '../api/cleaning';
import type { InputRun } from '../api/images';
import { periodLabel } from '../reportPeriod';

const props = defineProps<{
  run: InputRun;
  ready: boolean;
  sourceRevision: number;
  coreSelectionId: string;
  competitorSelectionId: string;
}>();
const emit = defineEmits<{
  saved: [];
  status: [value: 'waiting' | 'running' | 'review' | 'done'];
}>();
const history = ref<CleaningReport[]>([]);
const selectedId = ref('');
const report = computed(() => history.value.find((r) => r.id === selectedId.value));
const week = defineModel<string>('reportWeek', { required: true });
// 用户须确认只合并文本、不同周期指标不混算；确认标记不替代后端的来源覆盖校验。
const acknowledged = ref(false);
const busy = ref(false);
// Read-only restoration must not change saved execution status.
const reading = ref(true);
const classificationRetrying = ref(false);
const loadingRows = ref(false);
const error = ref('');
const progress = ref<ClassificationProgress | null>(null);
const progressUnavailable = ref(false);
const progressBlocked = computed(() => !!selectedId.value && (progressUnavailable.value || !progress.value || progress.value.status === 'running' || progress.value.status === 'outcome_pending'));
const rows = ref<CleaningPage | null>(null);
const kind = ref<'frequencies' | 'phrases'>('frequencies');
const offset = ref(0);
// 人工复核：一个词根可挂多个标签；保存时每个标签一行，exclusions 与原因一并保留。
const editing = ref(false);
const draftCategories = ref<Record<string, Category[]>>({});
const editReason = ref('');
const savingReview = ref(false);
const categories: { value: Category; label: string }[] = [
  { value: 'selling_point', label: '卖点词根' },
  { value: 'scene', label: '场景属性' },
  { value: 'preference', label: '偏好属性' },
  { value: 'unclassified', label: '未归类' },
];
let epoch = 0;
let rowEpoch = 0;
let alive = true;
let progressEpoch = 0;
let progressTimer: ReturnType<typeof setTimeout> | undefined;
onBeforeUnmount(() => {
  alive = false;
  epoch++;
  rowEpoch++;
  progressEpoch++;
  if (progressTimer) clearTimeout(progressTimer);
});
function message(e: unknown) {
  return e instanceof Error ? e.message : '操作失败，请重试';
}
const classification = (term: string) => report.value?.review?.roots.find((r) => r.term === term);
const classificationLabels = (term: string) =>
  (report.value?.review?.roots ?? [])
    .filter((root) => root.term === term)
    .map((root) => categories.find((c) => c.value === root.category)?.label ?? root.category)
    .join(' + ') || '待自动分类';
const draftCategory = (term: string): Category[] =>
  draftCategories.value[term] ?? [classification(term)?.category ?? 'unclassified'];
const categoryOrder: Category[] = ['selling_point', 'scene', 'preference', 'unclassified'];
function startEdit() {
  const current = report.value;
  if (!current?.review) return;
  const grouped: Record<string, Category[]> = {};
  for (const root of current.review.roots) {
    const list = grouped[root.term] ?? [];
    if (!list.includes(root.category)) list.push(root.category);
    grouped[root.term] = list;
  }
  draftCategories.value = grouped;
  editReason.value = '人工复核调整词根分类';
  editing.value = true;
  error.value = '';
}
function cancelEdit() {
  editing.value = false;
  draftCategories.value = {};
  editReason.value = '';
}
function toggleDraft(term: string, category: Category, checked: boolean) {
  const current = draftCategories.value[term] ?? [];
  let next: Category[];
  if (category === 'unclassified') {
    next = checked ? ['unclassified'] : [];
  } else if (checked) {
    next = [...current.filter((item) => item !== 'unclassified'), category];
  } else {
    next = current.filter((item) => item !== category);
  }
  // 至少保留一个标签，避免保存出没有类别的词根。
  if (!next.length) next = ['unclassified'];
  draftCategories.value = { ...draftCategories.value, [term]: next };
}
function toggleDraftEvent(term: string, category: Category, event: Event) {
  toggleDraft(term, category, (event.target as HTMLInputElement).checked);
}
async function saveReview() {
  const current = report.value;
  if (!current?.review) return;
  savingReview.value = true;
  error.value = '';
  try {
    const roots: RootReview[] = [];
    for (const [term, categories] of Object.entries(draftCategories.value)) {
      for (const category of categoryOrder) {
        if (!categories.includes(category)) continue;
        const existing = current.review.roots.find(
          (root) => root.term === term && root.category === category,
        );
        roots.push({
          term,
          category,
          evidence: existing?.evidence ?? '人工复核添加的标签',
        });
      }
    }
    await cleaningApi.review(props.run, current.id, {
      expected_version: current.version,
      roots,
      exclusions: current.review.exclusions,
      reason: editReason.value.trim() || '人工复核调整词根分类',
    });
    editing.value = false;
    draftCategories.value = {};
    await refresh(false);
    emit('saved');
  } catch (e) {
    error.value = message(e);
  } finally {
    savingReview.value = false;
  }
}
async function loadRows() {
  const current = ++rowEpoch;
  rows.value = null;
  if (!report.value) return;
  loadingRows.value = true;
  try {
    const result = await cleaningApi.rows(props.run, report.value.id, kind.value, offset.value);
    if (alive && current === rowEpoch) rows.value = result;
  } catch (e) {
    if (alive && current === rowEpoch) error.value = message(e);
  } finally {
    if (alive && current === rowEpoch) loadingRows.value = false;
  }
}
async function refresh(discard = true) {
  reading.value = true;
  const current = ++epoch;
  busy.value = true;
  error.value = '';
  try {
    const result = await cleaningApi.history(props.run);
    if (!alive || current !== epoch) return;
    history.value = result;
    selectedId.value = result.some((r) => r.id === selectedId.value)
      ? selectedId.value
      : (result[0]?.id ?? '');
    void discard;
    offset.value = 0;
    await loadRows();
  } catch (e) {
    if (alive && current === epoch) error.value = message(e);
  } finally {
    if (alive && current === epoch) { busy.value = false; reading.value = false; }
  }
}
function startProgressPolling() {
  const current = ++progressEpoch;
  if (progressTimer) clearTimeout(progressTimer);
  progress.value = null;
  progressUnavailable.value = false;
  const id = selectedId.value;
  const run = props.run;
  if (!id) return;
  const poll = async () => {
    try {
      const result = await cleaningApi.progress(run, id);
      if (!alive || current !== progressEpoch) return;
      progress.value = result;
      progressUnavailable.value = false;
      if (result.status === 'succeeded' && !report.value?.review) {
        const updated = await cleaningApi.history(run);
        if (alive && current === progressEpoch) history.value = updated;
      }
    } catch {
      if (!alive || current !== progressEpoch) return;
      progressUnavailable.value = true;
    }
    if (alive && current === progressEpoch && progress.value?.status !== 'succeeded')
      progressTimer = setTimeout(poll, 3000);
  };
  void poll();
}
async function generate() {
  const current = ++epoch;
  busy.value = true;
  error.value = '';
  try {
    const created = await cleaningApi.create(props.run, {
      aba_week_end: week.value,
      seller_period: 'last_30_days',
      seller_start: null,
      seller_end: null,
      text_only_acknowledged: acknowledged.value,
    });
    if (!alive || current !== epoch) return;
    history.value = [created, ...history.value.filter((x) => x.id !== created.id)];
    selectedId.value = created.id;
    const r = await cleaningApi.classify(props.run, created.id);
    if (!alive || current !== epoch) return;
    history.value = [r, ...history.value.filter((x) => x.id !== r.id)];
    selectedId.value = r.id;
    offset.value = 0;
    await loadRows();
    emit('saved');
  } catch (e) {
    if (alive && current === epoch) error.value = message(e);
  } finally {
    if (alive && current === epoch) busy.value = false;
  }
}
async function retryClassification() {
  if (!report.value || report.value.stale || !props.ready || busy.value || classificationRetrying.value || progress.value?.status !== 'failed') return;
  const id = report.value.id;
  const run = props.run;
  const current = ++epoch;
  busy.value = true;
  classificationRetrying.value = true;
  error.value = '';
  try {
    const result = await cleaningApi.retryClassification(run, id);
    if (!alive || current !== epoch) return;
    history.value = [result, ...history.value.filter((item) => item.id !== result.id)];
    await loadRows();
    emit('saved');
  } catch (e) {
    if (alive && current === epoch) error.value = message(e);
  } finally {
    if (alive && current === epoch) {
      busy.value = false;
      classificationRetrying.value = false;
    }
  }
}
async function download() {
  if (!report.value) return;
  try {
    await cleaningApi.download(props.run, report.value.id);
  } catch (e) {
    error.value = message(e);
  }
}
watch(selectedId, () => {
  // 切换快照时放弃未保存的分类修改，避免把草稿写到另一份快照上。
  cancelEdit();
  startProgressPolling();
  offset.value = 0;
  void loadRows();
});
watch(kind, () => {
  offset.value = 0;
  void loadRows();
});
watch(offset, () => {
  void loadRows();
});
watch(
  () => [props.run.id, props.coreSelectionId, props.competitorSelectionId, props.ready],
  (values, previous) => {
    rowEpoch++;
    if (previous && values[0] !== previous[0]) {
      epoch++;
      busy.value = false;
      classificationRetrying.value = false;
      progressEpoch++;
      if (progressTimer) clearTimeout(progressTimer);
      progress.value = null;
      progressUnavailable.value = false;
      history.value = [];
      selectedId.value = '';
    }
    if (!busy.value) void refresh(false);
  },
  { immediate: true },
);
watch(
  [report, reading, week, busy, error, () => props.ready],
  () => {
    if (reading.value) return;
    emit(
      'status',
      busy.value
        ? 'running'
        : report.value?.review &&
            report.value.request.aba_week_end === week.value &&
            report.value.request.seller_period === 'last_30_days' &&
            !report.value.stale &&
            props.ready &&
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
  () => props.sourceRevision,
  () => busy.value,
  async () => {
    selectedId.value = '';
    await refresh(false);
  },
);
</script>

<template>
  <section class="cleaning-panel" aria-label="词频分析与清洗">
    <p v-if="syncPending" role="status" class="sync-notice">
      后台结果有更新；当前操作或未保存修改结束后将自动同步。也可使用面板的读取按钮重新加载。
    </p>
    <header>
      <div>
        <p class="eyebrow">STEP 06 · 词频清洗</p>
        <h2>词频分析与清洗</h2>
        <p>汇总四路关键词，保存单词根清洗结果，并结合商品事实自动分类。</p>
      </div>
      <button :disabled="busy" @click="refresh()">读取分析历史</button>
    </header>
    <p v-if="!ready" class="notice">先完成并保存第二至五步；历史分析仍可查看。</p>
    <div class="dates">
      <label>ABA 报告周结束日<input v-model="week" type="date" :disabled="busy" /></label>
      <p class="muted">
        卖家精灵固定使用最近 30 天，保留每份词表的采集时间；只合并文本，不要求与 ABA 周起止日相同。
      </p>
    </div>
    <label class="ack"
      ><input
        v-model="acknowledged"
        type="checkbox"
        :disabled="busy"
      />确认仅合并关键词文本，各周期排名和份额独立保留，不混算指标。</label
    >
    <button class="primary" :disabled="busy || progressBlocked || classificationRetrying || !ready || !week || !acknowledged" @click="generate">
      汇总、清洗并自动分类
    </button>
    <div v-if="selectedId && (progress || progressUnavailable)" class="progress-box" role="status" aria-live="polite">
      <p v-if="progressUnavailable">无法读取分类进度，正在尝试重新连接；请勿重复提交。</p>
      <template v-else-if="progress">
        <p v-if="progress.status === 'running'">自动分类处理中：已保存 {{ progress.completed }} / {{ progress.total }} 个词根。若长时间无变化，请检查自动运行状态。</p>
        <p v-else-if="progress.status === 'failed'">自动分类失败，已保存 {{ progress.completed }} / {{ progress.total }} 个词根；可使用下方重试按钮继续。</p>
        <p v-else-if="progress.status === 'outcome_pending'">自动分类结果未知，已保存 {{ progress.completed }} / {{ progress.total }} 个词根；请勿重复提交，请先检查运行状态。</p>
        <p v-else-if="progress.status === 'succeeded'">自动分类完成：{{ progress.completed }} / {{ progress.total }} 个词根。</p>
        <p v-else>清洗快照已保存，自动分类尚未开始。</p>
        <progress v-if="progress.total > 0" :value="progress.completed" :max="progress.total" :aria-label="`已分类 ${progress.completed} / ${progress.total}`" />
      </template>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div v-if="progress?.status === 'failed' && report && !report.stale" class="retry-classification" role="group">
      <p>本次汇总与清洗快照已保留，可以直接从自动分类断点续跑，无需重复汇总。</p>
      <button
        type="button"
        class="primary"
        :disabled="busy || classificationRetrying || progressBlocked || !ready"
        @click="retryClassification"
      >
        {{ classificationRetrying ? '正在重新自动分类…' : '重新自动分类并继续' }}
      </button>
    </div>
    <details class="business-history">
      <summary>历史快照与版本选择</summary>
      <label v-if="history.length" class="history"
        >分析快照<select v-model="selectedId" :disabled="busy || progressBlocked">
          <option v-for="r in history" :key="r.id" :value="r.id">
            {{ r.created_at }} · {{ r.phrase_count }} 词 ·
            {{ r.stale ? '上游已变化' : r.review ? '已自动分类' : '待自动分类' }}
          </option>
        </select></label
      >
    </details>
    <article v-if="report">
      <p v-if="report.stale" class="notice">上游已变化，此快照仅供追溯，请重新生成分析。</p>
      <p v-else-if="progress?.status === 'failed'" class="notice" role="alert">
        部分词根分类失败，已完成结果与待补词均已保留，系统会自动延迟补跑；达到上限后可点下方按钮从断点继续。
      </p>
      <p v-else-if="report.review && ready" class="success">
        清洗结果与自动分类已保存，第六步已完成。
      </p>
      <p>
        {{ report.phrase_count }} 条关键词汇总记录 · {{ report.token_count }} 个英文单词 ·
        {{ report.frequency_count }} 个清洗后单词根
      </p>
      <p class="muted">
        {{ report.algorithm_version }} / {{ report.dictionary_version }} · 自动分类版本
        {{ report.version }}。保留单复数、拼写变体及长尾词；频率不代表购买意图。
      </p>
      <details>
        <summary>查看 {{ report.sources.length }} 份来源及周期</summary>
        <ul>
          <li v-for="s in report.sources" :key="s.id">
            {{ s.provider }} / {{ s.direction === 'forward' ? '正向' : '反向' }} / {{ s.query }} ·
            {{ periodLabel(s) }} · {{ s.id }}
          </li>
        </ul>
      </details>
      <div class="tools">
        <label
          >查看内容<select v-model="kind" :disabled="busy">
            <option value="frequencies">单词根清洗与自动分类</option>
            <option value="phrases">关键词汇总表</option>
          </select></label
        ><button @click="download">下载汇总、清洗与分类 CSV</button
        ><button
          v-if="report.review && !report.stale"
          type="button"
          :disabled="busy || savingReview"
          @click="editing ? cancelEdit() : startEdit()"
        >
          {{ editing ? '取消修改' : '修改分类' }}
        </button>
      </div>
      <div v-if="editing" class="edit-bar" role="group" aria-label="人工复核分类">
        <p class="muted">
          保存后生成新的审核版本，已发布的词库快照不受影响；只改类别，不改词根与证据。
        </p>
        <label
          >修改原因<input
            v-model="editReason"
            maxlength="200"
            :disabled="savingReview"
            placeholder="例如：adjustable 应归卖点词根"
        /></label>
        <button class="primary" :disabled="savingReview || !editReason.trim()" @click="saveReview">
          {{ savingReview ? '保存中…' : '保存分类' }}
        </button>
      </div>
      <p class="muted">
        词频按全部汇总记录计算，不先去重。密度＝出现次数 / 全部英文单词数；只保留单个英文词根。
      </p>
      <p v-if="loadingRows">正在读取…</p>
      <div v-else-if="rows" class="table-scroll">
        <table v-if="kind === 'frequencies'">
          <thead>
            <tr>
              <th>候选词根</th>
              <th>次数</th>
              <th>覆盖短语</th>
              <th>密度</th>
              <th>自动分类与商品依据</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="f in rows.frequencies" :key="f.term">
              <td>
                {{ f.term
                }}<small v-if="f.suggestions.length"
                  >建议：{{
                    categories
                      .filter((c) => f.suggestions.includes(c.value))
                      .map((c) => c.label)
                      .join('、')
                  }}</small
                >
              </td>
              <td>{{ f.occurrences }}</td>
              <td>{{ f.phrase_count }}</td>
              <td>{{ f.density.toFixed(2) }}%</td>
              <td>
                <template v-if="editing">
                  <label v-for="c in categories" :key="c.value" class="tag">
                    <input
                      type="checkbox"
                      :checked="draftCategory(f.term).includes(c.value)"
                      :disabled="savingReview"
                      @change="toggleDraftEvent(f.term, c.value, $event)"
                    />{{ c.label }}
                  </label>
                </template>
                <template v-else>
                  <strong>{{ classificationLabels(f.term) }}</strong>
                  <small>{{ classification(f.term)?.evidence || '—' }}</small>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
        <table v-else>
          <thead>
            <tr>
              <th>关键词</th>
              <th>来源证据</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(p, rowIndex) in rows.phrases" :key="`${p.keyword}-${rowIndex}`">
              <td>{{ p.keyword }}</td>
              <td>
                <details>
                  <summary>{{ p.evidence.length }} 条证据</summary>
                  <div v-for="(e, i) in p.evidence" :key="i">
                    <p>
                      {{ e.original }} · 查询 {{ e.query }} · 来源行 / 证据序号 {{ e.source_row }} ·
                      {{ e.source_id }}
                    </p>
                    <pre>{{ e.payload }}</pre>
                  </div>
                </details>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="rows" class="pagination">
        <span>共 {{ rows.total }} 条 · 第 {{ Math.floor(offset / 20) + 1 }} 页</span
        ><button :disabled="busy || loadingRows || offset === 0" @click="offset -= 20">
          上一页</button
        ><button :disabled="busy || loadingRows || offset + 20 >= rows.total" @click="offset += 20">
          下一页
        </button>
      </div>
    </article>
  </section>
</template>

<style scoped>
.progress-box { margin: 14px 0; padding: 12px 16px; border: 1px solid #bfdbfe; border-radius: 10px; background: #eff6ff; color: #1e3a5f; }
.progress-box p { margin: 0 0 8px; }
.progress-box progress { width: 100%; height: 12px; accent-color: #2563eb; }

.cleaning-panel {
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 16px;
  padding: 24px;
  margin: 20px 0;
  color: #23334a;
  min-width: 0;
}
.cleaning-panel header,
.tools,
.pagination {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.retry-classification {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 12px;
  padding: 12px 14px;
  border: 1px solid #fecaca;
  border-radius: 10px;
  background: #fff7f7;
}
.retry-classification p {
  flex: 1 1 420px;
  margin: 0;
  color: #b42318;
}
h2 {
  margin: 4px 0 10px;
  font-size: 21px;
}
.eyebrow {
  font-size: 11px;
  color: #4f46e5;
  letter-spacing: 1px;
}
.dates {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin: 18px 0;
}
.dates label,
.reason,
.history,
.tools label,
.exclusion label {
  display: flex;
  flex-direction: column;
  gap: 7px;
  font-size: 13px;
}
input:not([type='checkbox']),
select,
textarea {
  border: 1px solid #cbd5e1;
  border-radius: 7px;
  padding: 9px;
  font: inherit;
  max-width: 100%;
  box-sizing: border-box;
}
input[type='checkbox'] {
  accent-color: #4f46e5;
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
.ack {
  display: flex;
  gap: 8px;
  font-size: 13px;
  line-height: 1.6;
}
.error {
  color: #b42318;
}
.notice {
  color: #9a6700;
}
.success {
  color: #15803d;
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
.history {
  margin: 16px 0;
}
.tools {
  margin: 20px 0;
}
.edit-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  margin: 0 0 12px;
  border: 1px solid #dbe3ec;
  border-radius: 8px;
}
.edit-bar p {
  flex: 1 1 100%;
  margin: 0;
  font-size: 12px;
}
.edit-bar input {
  min-width: 240px;
}
.tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-right: 10px;
  font-size: 12px;
  white-space: nowrap;
}
.table-scroll {
  overflow: auto;
  max-height: 550px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
th,
td {
  text-align: left;
  padding: 12px;
  border-bottom: 1px solid #edf0f4;
  vertical-align: top;
}
th {
  background: #f8fafc;
  white-space: nowrap;
}
.classification {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-right: 8px;
  white-space: nowrap;
}
.pagination {
  margin: 12px 0;
  font-size: 12px;
  justify-content: flex-end;
}
.reason {
  margin: 14px 0;
}
.reason textarea {
  min-height: 80px;
}
.exclusion {
  display: flex;
  gap: 10px;
  margin: 10px 0;
  align-items: flex-end;
}
.exclusion label {
  flex: 1;
}
details {
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-width: 460px;
  font-size: 11px;
}
article {
  min-width: 0;
}
li {
  overflow-wrap: anywhere;
}
@media (max-width: 600px) {
  .cleaning-panel {
    padding: 16px;
  }
  .dates label {
    width: 100%;
  }
  .history select {
    width: 100%;
  }
  .tools {
    align-items: stretch;
  }
  table {
    min-width: 600px;
  }
  .exclusion {
    flex-direction: column;
    align-items: stretch;
  }
  header button {
    width: 100%;
  }
}
</style>

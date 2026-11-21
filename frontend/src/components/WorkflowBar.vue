<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import type { InputRun } from '../api/images';
import type { ProductTermAnalysis } from '../api/productTermAnalysis';
import {
  workflowApi,
  type WorkflowEventView,
  type WorkflowNodeView,
  type WorkflowView,
} from '../api/workflow';
import { listZiniaoStores, selectZiniaoStore, type ZiniaoStore } from '../api/ziniaoStores';
import { resolveBrowserDeviceId } from '../browserDevice';

const props = defineProps<{
  run: InputRun;
  savedAction?: { id: number; gate: string };
  historyPreview?: boolean;
  termAnalysis?: ProductTermAnalysis | null;
}>();
const emit = defineEmits<{ view: [value: WorkflowView | null] }>();
const view = ref<WorkflowView | null>(null);
const timeline = ref<HTMLElement | null>(null);
const clock = ref(Date.now());
const busy = ref(false);
const error = ref('');
const week = defineModel<string>('reportWeek', { required: true });
const autoDecide = ref(true);
// 历史预览先只读；只有明确继续/启动后才允许自动恢复外部等待，避免打开记录就采集。
const allowAutoResume = ref(!props.historyPreview);
let timer: number | undefined;
let alive = true;
// 请求序号防止较早轮询的迟到响应覆盖后来启动/恢复操作返回的新状态。
let requestEpoch = 0;
let lastViewKey = '';
function publish(next: WorkflowView | null) {
  if (!alive) return;
  clock.value = Date.now();
  view.value = next;
  const key = JSON.stringify(next);
  if (key !== lastViewKey) {
    lastViewKey = key;
    emit('view', next);
  }
}

const statusLabels: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  waiting_human: '等待人工',
  waiting_external: '等待采集',
  completed: '已完成',
  failed: '失败',
  interrupted: '已中断',
  superseded: '已被取代',
  aborted: '已终止',
  incompatible: '版本不兼容',
};
const closed = ['completed', 'failed', 'superseded', 'aborted', 'incompatible'];
const message = (e: unknown) => (e instanceof Error ? e.message : '编排操作失败');
const statusLabel = (value: string) => statusLabels[value] ?? value;
const active = computed(() => !!view.value && !closed.includes(view.value.status));
const nodeLabel = (key: string) => view.value?.nodes.find((node) => node.key === key)?.label ?? key;

// 紫鸟店铺选择：多店铺时采集端无法自动判断，必须由用户指定。
const stores = ref<ZiniaoStore[]>([]);
const storeChoice = ref('');
const storesLoading = ref(false);
const storesError = ref('');
const showStorePanel = computed(() =>
  ['waiting_human', 'waiting_external', 'failed'].includes(view.value?.status ?? ''),
);
const needsStoreChoice = computed(
  () => view.value?.waiting_for?.refs?.needs_store_choice === '1',
);

async function loadStores(): Promise<void> {
  storesLoading.value = true;
  storesError.value = '';
  try {
    const result = await listZiniaoStores();
    stores.value = result.stores ?? [];
    storeChoice.value = result.current ?? stores.value[0]?.id ?? '';
    if (!stores.value.length && result.message) storesError.value = result.message;
  } catch (e) {
    storesError.value = message(e);
  } finally {
    storesLoading.value = false;
  }
}

async function applyStore(): Promise<void> {
  if (!storeChoice.value) return;
  // 选店与继续共用一次 busy 锁，避免 resume 把自己的操作当成重复点击。
  await resume('continue', {}, () => selectZiniaoStore(storeChoice.value));
}

watch([needsStoreChoice, showStorePanel], ([needed, shown]) => {
  if ((needed || shown) && !stores.value.length) void loadStores();
});
const finishedNodeStatuses = new Set(['done', 'skipped']);
const finishedNodes = computed(
  () => view.value?.nodes.filter((node) => finishedNodeStatuses.has(node.status)).length ?? 0,
);
const totalNodes = computed(() => view.value?.nodes.length ?? 0);
const progressPercent = computed(() =>
  totalNodes.value ? Math.round((finishedNodes.value / totalNodes.value) * 100) : 0,
);
const currentEvent = computed(() =>
  [...(view.value?.events ?? [])]
    .reverse()
    .find((event) => ['running', 'waiting_human', 'waiting_external'].includes(event.status)),
);
const eventStatusLabels: Record<string, string> = {
  running: '执行中',
  waiting_human: '等待人工',
  waiting_external: '等待采集',
  done: '已完成',
  failed: '失败',
  skipped: '已跳过',
  interrupted: '已中断',
};
const eventIcons: Record<string, string> = {
  running: '●',
  waiting_human: '◆',
  waiting_external: '◆',
  done: '✓',
  failed: '×',
  skipped: '–',
  interrupted: '!',
};
const nodeStatusLabels: Record<string, string> = {
  pending: '未执行',
  running: '执行中',
  waiting_human: '等待人工',
  waiting_external: '等待采集',
  done: '已完成',
  failed: '失败',
  skipped: '已跳过',
};
const nodePurposes: Record<string, string> = {
  ensure_input: '校验 ASIN、图片、站点和基础输入是否完整。',
  identify_product: '读取或识别商品名称、类型、属性和参考价格。',
  product_confirmation: '确认商品画像，作为后续相关性判断依据。',
  generate_roots: '根据商品资料生成并核验候选词根。',
  root_selection: '确认后续检索使用的词根或商品词。',
  week: '确定 ABA 报告周和各数据源的采集口径。',
  collect_core_report: '按报告周采集 ABA 热门搜索词报告。',
  core_terms: '分析候选词并保存符合规则的核心大词。',
  competitor_discovery: '搜索并汇总候选竞对 ASIN。',
  competitor_selection: '根据相似度、价格等条件确认竞对。',
  submit_sources: '提交 ABA 正反向和第三方拓词任务。',
  wait_sources: '等待各来源采集完成并检查覆盖情况。',
  third_party: '采集或导入卖家精灵最近 30 天词表。',
  create_cleaning: '汇总来源、标准化关键词并生成清洗结果。',
  cleaning_review: '审核词根分类和关键词清洗结果。',
  create_traffic: '结合 ABA 指标生成流量筛选建议。',
  traffic_review: '审核关键词相关性、优先级和保留结果。',
  create_library: '生成关键词分层、辅助 ASIN 和入库字段。',
  library_publish: '人工确认并发布关键词库版本。',
};
const nodePurpose = (key: string) => nodePurposes[key] ?? '执行该工作流节点的业务处理。';
const legacyNodeResult = (node: WorkflowNodeView) => {
  if (node.detail) return node.detail;
  if (node.status === 'pending') return '尚未执行。';
  if (node.status === 'waiting_human') return '等待人工处理；旧记录未保存更多输入信息。';
  if (node.status === 'waiting_external') return '等待外部采集；旧记录未保存更多进度信息。';
  if (node.status === 'running') return '记录结束时仍在执行；旧记录未保存过程日志。';
  if (node.status === 'skipped') return '本步已跳过；旧记录未保存更多原因。';
  if (node.status === 'failed') return '本步执行失败；旧记录未保存更多错误信息。';
  return '本步已完成；旧记录未保存输出摘要和耗时。';
};
const formatTime = (value: string) =>
  new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value));
const formatDuration = (milliseconds: number | null) => {
  if (milliseconds === null) return '';
  if (milliseconds < 1000) return `${milliseconds} 毫秒`;
  const seconds = Math.round(milliseconds / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
};
const runningDuration = (event: WorkflowEventView | undefined) =>
  event ? Math.max(0, clock.value - new Date(event.started_at).getTime()) : null;
const entries = (summary: Record<string, string>) => Object.entries(summary);

function schedule(ms: number) {
  window.clearTimeout(timer);
  if (!alive || !active.value) return;
  timer = window.setTimeout(() => void poll(), Math.max(ms, 1000));
}

async function poll() {
  clock.value = Date.now();
  if (busy.value) {
    schedule(1500);
    return;
  }
  const token = ++requestEpoch;
  try {
    const next = await workflowApi.latest(props.run);
    if (!alive || token !== requestEpoch) return;
    publish(next);
    if (!next) return;
    if (
      allowAutoResume.value &&
      // 后端确认 ready 后才 poll 恢复；waiting_human 必须走保存或明确继续动作。
      next.status === 'waiting_external' &&
      next.waiting_for?.ready_to_resume
    ) {
      await resume('poll', {});
      return;
    }
    if (['queued', 'running', 'waiting_human', 'waiting_external'].includes(next.status)) {
      schedule(next.next_poll_ms || 3000);
    }
  } catch (e) {
    if (alive) {
      error.value = message(e);
      schedule(5000);
    }
  }
}

// Only an explicit successful business write may advance a human gate.
watch(
  () => props.savedAction,
  (action) => {
    const current = view.value;
    if (!action || current?.status !== 'waiting_human' || !current.waiting_for) return;
    const gate = current.waiting_for.gate;
    if (
      action.gate === gate ||
      (gate === 'competitor_discovery' && action.gate === 'competitor_selection')
    ) {
      void resume('continue', {});
    }
  },
);
watch(
  () => {
    const events = view.value?.events ?? [];
    const latestStatus = events.at(-1)?.status;
    return latestStatus ? `${events.length}:${latestStatus}` : '0';
  },
  async () => {
    await nextTick();
    timeline.value?.scrollTo({ top: timeline.value.scrollHeight, behavior: 'smooth' });
  },
);

async function resume(
  action: 'continue' | 'poll',
  payload: Record<string, string>,
  beforeResume?: () => Promise<unknown>,
) {
  const current = view.value;
  if (!current?.waiting_for || busy.value) return;
  allowAutoResume.value = true;
  busy.value = true;
  const token = ++requestEpoch;
  error.value = '';
  try {
    await beforeResume?.();
    const next = await workflowApi.resume(props.run, current.workflow_id, {
      gate: current.waiting_for.gate,
      action,
      expected_revision: current.revision,
      payload,
    });
    if (!alive || token !== requestEpoch) return;
    publish(next);
    schedule(next.next_poll_ms);
  } catch (e) {
    error.value = message(e);
  } finally {
    busy.value = false;
  }
}

const cleaningModelFailure = computed(
  () =>
    view.value?.status === 'failed' &&
    view.value.nodes.some((node) => node.key === 'cleaning_review' && node.status === 'failed') &&
    !!view.value.error.startsWith('MODEL_OUTPUT_INVALID'),
);
const trafficFailure = computed(
  () =>
    view.value?.status === 'failed' &&
    view.value.nodes.some((node) => node.key === 'traffic_review' && node.status === 'failed'),
);
async function retryTraffic() {
  const current = view.value;
  if (!current || busy.value) return;
  busy.value = true;
  const token = ++requestEpoch;
  error.value = '';
  try {
    const next = await workflowApi.retryTraffic(props.run, current.workflow_id, current.revision);
    if (!alive || token !== requestEpoch) return;
    publish(next);
    schedule(next.next_poll_ms);
  } catch (e) {
    error.value = message(e);
  } finally {
    busy.value = false;
  }
}

async function start(manualCleaning = false, reanalyzeCore = false) {
  allowAutoResume.value = true;
  busy.value = true;
  const token = ++requestEpoch;
  error.value = '';
  try {
    const next = await workflowApi.start(
      props.run,
      {
        auto_decide: reanalyzeCore || autoDecide.value,
        report_week_end: week.value,
        third_party_mode: reanalyzeCore || autoDecide.value ? 'auto' : 'manual',
        manual_cleaning_review: manualCleaning,
        reanalyze_core: reanalyzeCore,
      },
      // 把采集设备一起带上去：服务器据此把浏览器操作发到这台电脑的紫鸟。
      resolveBrowserDeviceId(),
    );
    if (!alive || token !== requestEpoch) return;
    publish(next);
    schedule(next.next_poll_ms);
  } catch (e) {
    error.value = message(e);
  } finally {
    busy.value = false;
  }
}

async function proceed() {
  const current = view.value;
  if (!current?.waiting_for) return;
  if (current.waiting_for.gate === 'week' && !/^\d{4}-\d{2}-\d{2}$/.test(week.value)) {
    error.value = '请选择报告周结束日（周六）';
    return;
  }
  await resume('continue', current.waiting_for.gate === 'week' ? { aba_week_end: week.value } : {});
}

async function abort() {
  const current = view.value;
  if (!current) return;
  busy.value = true;
  const token = ++requestEpoch;
  try {
    const next = await workflowApi.abort(props.run, current.workflow_id);
    if (alive && token === requestEpoch) publish(next);
  } catch (e) {
    error.value = message(e);
  } finally {
    busy.value = false;
  }
}

watch(
  () => props.run.id,
  () => {
    requestEpoch++;
    allowAutoResume.value = !props.historyPreview;
    view.value = null;
    lastViewKey = '';
    emit('view', null);
    void poll();
  },
);
onMounted(() => void poll());
onBeforeUnmount(() => {
  alive = false;
  window.clearTimeout(timer);
});
</script>

<template>
  <section class="workflow-bar" aria-label="编排状态">
    <div class="head">
      <strong>自动运行</strong>
      <span v-if="view" class="pill" :data-status="view.status">{{
        statusLabel(view.status)
      }}</span>
      <span v-if="view?.current_node" class="muted"
        >当前节点：{{ nodeLabel(view.current_node) }}</span
      >
    </div>
    <p v-if="termAnalysis && view?.current_node === 'root_selection'" class="muted" aria-live="polite">
      商品词已处理 {{ termAnalysis.items.filter(item => ['completed', 'no_samples', 'failed'].includes(item.status)).length }}/{{ termAnalysis.items.length }} 个，{{ termAnalysis.best_candidate_ids.length }} 个达到门槛。
      <span v-for="item in termAnalysis.items.filter(item => item.status === 'running')" :key="item.candidate_id">
        当前 {{ item.text }}：{{ item.phase === 'scoring' ? '模型评分' : item.phase === 'collecting' ? '采集详情' : '读取搜索页' }}，
        详情 {{ item.collected_products ?? 0 }}/{{ item.found_products ?? 0 }}，评分 {{ item.scored_products ?? 0 }}/{{ item.collected_products ?? 0 }}。
      </span>
    </p>
    <p v-if="!view" class="muted">
      可启动自动运行，系统依次处理已有输入；需要人工确认时暂停，请前往顶部提示的步骤处理。
    </p>
    <div v-if="view" class="overview">
      <div class="progress-copy">
        <strong>总体进度</strong>
        <span>{{ finishedNodes }} / {{ totalNodes }} 步 · {{ progressPercent }}%</span>
      </div>
      <progress :value="finishedNodes" :max="totalNodes || 1">{{ progressPercent }}%</progress>
      <div class="current-step">
        <span class="muted">当前步骤</span>
        <strong>{{
          view.current_node ? nodeLabel(view.current_node) : statusLabel(view.status)
        }}</strong>
        <span v-if="currentEvent" class="muted"
          >已运行 {{ formatDuration(runningDuration(currentEvent)) }}</span
        >
      </div>
    </div>
    <section v-if="view?.events.length" class="log-panel" aria-label="执行日志">
      <div class="log-head">
        <strong>执行日志</strong>
        <span class="muted">共 {{ view.events.length }} 条，自动滚动到最新结果</span>
      </div>
      <ol ref="timeline" class="timeline">
        <li
          v-for="event in view.events"
          :key="event.sequence"
          class="event"
          :data-status="event.status"
        >
          <span class="event-icon" aria-hidden="true">{{ eventIcons[event.status] }}</span>
          <div class="event-body">
            <div class="event-title">
              <time :datetime="event.started_at">{{ formatTime(event.started_at) }}</time>
              <strong>{{ event.node_label }}</strong>
              <span class="event-status">{{
                eventStatusLabels[event.status] ?? event.status
              }}</span>
              <span v-if="event.attempt > 1" class="attempt">第 {{ event.attempt }} 次尝试</span>
              <span class="duration">
                {{
                  formatDuration(
                    event.duration_ms ??
                      (event.status === 'running' ? runningDuration(event) : null),
                  )
                }}
              </span>
            </div>
            <p class="event-message">{{ event.message || '节点状态已更新' }}</p>
            <details class="event-details">
              <summary>查看输入、输出与错误</summary>
              <div class="event-detail-grid">
                <section>
                  <h4>输入摘要</h4>
                  <dl v-if="entries(event.input_summary).length">
                    <template v-for="[key, value] in entries(event.input_summary)" :key="key">
                      <dt>{{ key }}</dt>
                      <dd>{{ value }}</dd>
                    </template>
                  </dl>
                  <p v-else class="muted">无输入摘要</p>
                </section>
                <section>
                  <h4>输出结果</h4>
                  <dl v-if="entries(event.output_summary).length">
                    <template v-for="[key, value] in entries(event.output_summary)" :key="key">
                      <dt>{{ key }}</dt>
                      <dd>{{ value }}</dd>
                    </template>
                  </dl>
                  <p v-else class="muted">尚无输出结果</p>
                </section>
                <section v-if="event.error" class="event-error">
                  <h4>错误信息</h4>
                  <p>{{ event.error }}</p>
                </section>
              </div>
            </details>
          </div>
        </li>
      </ol>
    </section>
    <section v-else-if="view" class="legacy-log-panel" aria-label="旧记录步骤回放">
      <div class="log-head">
        <strong>旧记录步骤回放</strong>
        <span class="muted"
          >输入、输出和耗时等详细过程，旧记录未保存；以下内容来自当时保存的节点状态。</span
        >
      </div>
      <ol class="legacy-steps">
        <li
          v-for="node in view.nodes"
          :key="node.key"
          class="legacy-step"
          :data-status="node.status"
        >
          <span class="legacy-index">{{ view.nodes.indexOf(node) + 1 }}</span>
          <div class="legacy-step-body">
            <div class="legacy-title">
              <strong>{{ node.label }}</strong>
              <span class="event-status">{{ nodeStatusLabels[node.status] ?? node.status }}</span>
            </div>
            <div class="legacy-detail-grid">
              <div>
                <small>步骤作用</small>
                <p>{{ nodePurpose(node.key) }}</p>
              </div>
              <div>
                <small>执行结果</small>
                <p :class="{ 'legacy-error': node.status === 'failed' }">
                  {{ node.detail || legacyNodeResult(node) }}
                </p>
              </div>
            </div>
          </div>
        </li>
      </ol>
    </section>
    <p v-if="view?.waiting_for" class="waiting">
      <strong>{{ view.waiting_for.title }}</strong>
      <span>{{ view.waiting_for.instructions }}</span>
    </p>
    <div v-if="needsStoreChoice || showStorePanel" class="store-picker">
      <strong>{{ needsStoreChoice ? '请选择要采集的紫鸟店铺后继续' : '紫鸟店铺（随时可换）' }}</strong>
      <span v-if="storesLoading">正在读取这台电脑的紫鸟店铺…</span>
      <span v-else-if="storesError" class="error">{{ storesError }}</span>
      <template v-else>
        <select v-model="storeChoice" :disabled="busy">
          <option v-for="item in stores" :key="item.id" :value="item.id">
            {{ item.name || item.id }}{{ item.platform ? '（' + item.platform + '）' : '' }}
          </option>
        </select>
        <button type="button" :disabled="busy || !storeChoice" @click="applyStore">
          用这个店铺继续
        </button>
        <button type="button" class="ghost" :disabled="busy" @click="loadStores">重新读取</button>
      </template>
    </div>
    <p v-if="view?.error" role="alert" class="error">{{ view.error }}</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <div class="actions">
      <label v-if="!view" class="toggle"
        ><input
          v-model="autoDecide"
          type="checkbox"
          :disabled="busy"
        />自动判断（判断类交给模型）</label
      >
      <button v-if="!view" type="button" :disabled="busy" @click="start()">启动自动运行</button>
      <template v-if="view?.status === 'waiting_human'">
        <input
          v-if="view.waiting_for?.gate === 'week'"
          v-model="week"
          type="date"
          :disabled="busy"
        />
        <button type="button" :disabled="busy" @click="proceed">重新检查本步，继续运行</button>
      </template>
      <template v-if="view?.status === 'waiting_external'">
        <template v-if="!allowAutoResume">
          <span class="muted">历史回看：只读取状态；继续后可能采集数据或调用模型。</span>
          <button type="button" :disabled="busy" @click="resume('poll', {})">继续自动运行</button>
        </template>
        <span v-else class="muted">采集中，完成后自动继续…</span>
      </template>
      <button v-if="active && view" type="button" class="ghost" :disabled="busy" @click="abort">
        终止
      </button>
      <button v-if="cleaningModelFailure" type="button" :disabled="busy" @click="start(true)">
        改为人工审核清洗，保留已有结果
      </button>
      <button v-if="trafficFailure" type="button" :disabled="busy" @click="retryTraffic">
        继续剩余流量审核
      </button>
      <button v-if="view && !active" type="button" :disabled="busy" @click="start()">
        重新启动自动运行
      </button>
      <button v-if="view && !active" type="button" :disabled="busy" @click="start(false, true)">
        从核心大词重新分析
      </button>
      <small v-if="view && !active" class="muted"
        >重新分析使用已有周报；保存新核心词后，会重新检查并处理受影响的下游步骤。</small
      >
    </div>
  </section>
</template>

<style scoped>
.workflow-bar {
  display: grid;
  gap: 10px;
  padding: 16px 18px;
  border: 1px solid #e2e8f0;
  border-radius: 14px;
  background: #f8fafc;
}
.head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.overview {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) minmax(260px, 2fr) minmax(180px, 1fr);
  align-items: center;
  gap: 14px;
  padding: 13px 14px;
  border: 1px solid #dbe5f0;
  border-radius: 12px;
  background: #fff;
}
.progress-copy,
.current-step {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
progress {
  width: 100%;
  height: 10px;
  overflow: hidden;
  border: 0;
  border-radius: 999px;
  accent-color: #2563eb;
}
.log-panel {
  overflow: hidden;
  border: 1px solid #dbe5f0;
  border-radius: 12px;
  background: #fff;
}
.legacy-log-panel {
  overflow: hidden;
  border: 1px solid #dbe5f0;
  border-radius: 12px;
  background: #fff;
}
.legacy-steps {
  max-height: 520px;
  margin: 0;
  padding: 8px 14px 14px;
  overflow-y: auto;
  list-style: none;
}
.legacy-step {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr);
  gap: 10px;
  padding: 10px 0;
  border-bottom: 1px solid #edf2f7;
}
.legacy-step:last-child {
  border-bottom: 0;
}
.legacy-index {
  display: grid;
  width: 26px;
  height: 26px;
  place-items: center;
  border-radius: 50%;
  background: #e2e8f0;
  color: #475569;
  font-size: 12px;
  font-weight: 700;
}
.legacy-step[data-status='done'] .legacy-index {
  background: #dcfce7;
  color: #166534;
}
.legacy-step[data-status='failed'] .legacy-index {
  background: #fee2e2;
  color: #991b1b;
}
.legacy-step[data-status='skipped'] .legacy-index {
  background: #f1f5f9;
  color: #64748b;
}
.legacy-step-body,
.legacy-title {
  min-width: 0;
}
.legacy-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.legacy-detail-grid {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(260px, 1.4fr);
  gap: 12px;
  margin-top: 7px;
}
.legacy-detail-grid small {
  color: #94a3b8;
}
.legacy-detail-grid p {
  margin: 2px 0 0;
  color: #475569;
  font-size: 13px;
  overflow-wrap: anywhere;
}
.legacy-detail-grid .legacy-error {
  color: #b91c1c;
}
.log-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 14px;
  border-bottom: 1px solid #e2e8f0;
}
.timeline {
  max-height: 430px;
  margin: 0;
  padding: 8px 14px 14px;
  overflow-y: auto;
  list-style: none;
}
.event {
  position: relative;
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr);
  gap: 8px;
  padding: 8px 0;
}
.event:not(:last-child)::before {
  position: absolute;
  top: 30px;
  bottom: -8px;
  left: 11px;
  width: 2px;
  background: #e2e8f0;
  content: '';
}
.event-icon {
  z-index: 1;
  display: grid;
  width: 24px;
  height: 24px;
  place-items: center;
  border-radius: 50%;
  background: #e2e8f0;
  color: #475569;
  font-weight: 700;
}
.event[data-status='done'] .event-icon {
  background: #dcfce7;
  color: #166534;
}
.event[data-status='running'] .event-icon,
.event[data-status='waiting_human'] .event-icon,
.event[data-status='waiting_external'] .event-icon {
  background: #dbeafe;
  color: #1d4ed8;
}
.event[data-status='failed'] .event-icon,
.event[data-status='interrupted'] .event-icon {
  background: #fee2e2;
  color: #991b1b;
}
.event-body {
  min-width: 0;
}
.event-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.event-title time,
.duration,
.attempt,
.event-status {
  color: #64748b;
  font-size: 12px;
}
.event-message {
  margin: 3px 0 0;
  color: #475569;
  font-size: 13px;
}
.event-details {
  margin-top: 6px;
  color: #475569;
  font-size: 12px;
}
.event-details summary {
  cursor: pointer;
  color: #2563eb;
}
.event-detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 8px;
  padding: 10px;
  border-radius: 8px;
  background: #f8fafc;
}
.event-detail-grid h4 {
  margin: 0 0 5px;
  color: #334155;
}
.event-detail-grid dl {
  display: grid;
  grid-template-columns: minmax(70px, auto) minmax(0, 1fr);
  gap: 3px 8px;
  margin: 0;
}
.event-detail-grid dt {
  color: #64748b;
}
.event-detail-grid dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
}
.event-error {
  grid-column: 1 / -1;
  color: #b91c1c;
}
.event-error p {
  margin: 0;
}
.pill {
  padding: 2px 10px;
  border-radius: 999px;
  background: #e2e8f0;
  font-size: 12px;
}
.pill[data-status='completed'] {
  background: #dcfce7;
  color: #166534;
}
.pill[data-status='failed'],
.pill[data-status='superseded'],
.pill[data-status='aborted'] {
  background: #fee2e2;
  color: #991b1b;
}
.pill[data-status='waiting_human'],
.pill[data-status='waiting_external'],
.pill[data-status='interrupted'] {
  background: #fef3c7;
  color: #92400e;
}
.nodes {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.nodes li {
  padding: 3px 9px;
  border-radius: 8px;
  background: #eef2f7;
  color: #64748b;
  font-size: 12px;
}
.nodes li.done {
  background: #dcfce7;
  color: #166534;
}
.nodes li.skipped {
  background: #e2e8f0;
  color: #475569;
}
.nodes li.running,
.nodes li.waiting_human,
.nodes li.waiting_external {
  background: #fef3c7;
  color: #92400e;
}
.nodes li.failed {
  background: #fee2e2;
  color: #991b1b;
}
.waiting {
  display: grid;
  gap: 2px;
  margin: 0;
}
.store-picker {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 12px;
  border: 1px solid #dbe5f0;
  border-radius: 9px;
  background: #fafbfc;
  font-size: 12px;
  color: #475569;
}
.store-picker strong {
  color: #1e293b;
}
.store-picker select {
  height: 32px;
  padding: 0 8px;
  border: 1px solid #dbe3ec;
  border-radius: 7px;
  background: #fff;
  color: #1e293b;
  font-size: 13px;
}
.store-picker button {
  padding: 6px 12px;
  border: 1px solid #dbe3ec;
  border-radius: 7px;
  background: #fff;
  color: #475569;
  font-size: 12px;
  cursor: pointer;
}
.store-picker button:disabled {
  opacity: 0.55;
  cursor: default;
}
.actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.error {
  margin: 0;
  color: #b91c1c;
}
.muted {
  color: #64748b;
  font-size: 13px;
}
.ghost {
  background: transparent;
}
.toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: #475569;
}
@media (max-width: 780px) {
  .overview,
  .event-detail-grid,
  .legacy-detail-grid {
    grid-template-columns: 1fr;
  }
}
</style>

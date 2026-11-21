<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, provide, ref, watch } from 'vue';
import {
  Activity,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Copy,
  FileImage,
  FolderOpen,
  ImagePlus,
  Info,
  Layers3,
  LoaderCircle,
  Sparkles,
  X,
} from '@lucide/vue';
import RootVerificationPanel from './components/RootVerificationPanel.vue';
import ConfirmedProductTerms from './components/ConfirmedProductTerms.vue';
import ConfirmedCoreTerms from './components/ConfirmedCoreTerms.vue';
import CoreTermsPanel from './components/CoreTermsPanel.vue';
import CompetitorsPanel from './components/CompetitorsPanel.vue';
import AbaExpansionPanel from './components/AbaExpansionPanel.vue';
import ThirdPartyPanel from './components/ThirdPartyPanel.vue';
import CleaningPanel from './components/CleaningPanel.vue';
import TrafficPanel from './components/TrafficPanel.vue';
import ProductConfirmationPanel from './components/ProductConfirmationPanel.vue';
import ProductProfilePanel from './components/ProductProfilePanel.vue';
import ProductTermAnalysisPanel from './components/ProductTermAnalysisPanel.vue';
import ProductTermCandidatesPanel from './components/ProductTermCandidatesPanel.vue';
import {
  readProductTermAnalysis,
  startProductTermAnalysis,
  type ProductTermAnalysis,
} from './api/productTermAnalysis';
import WorkflowBar from './components/WorkflowBar.vue';
import WorkflowWorkspace from './components/WorkflowWorkspace.vue';
import ResearchRecords from './components/ResearchRecords.vue';
import ModelSettings from './components/ModelSettings.vue';
import { steps as workflowSteps, currentStep, stepForNode } from './workflowGuide';
import { usePanelSync } from './panelSync';
import { readResultSummary } from './api/resultSummary';
import type { WorkflowView } from './api/workflow';
import { latestReportWeek } from './reportPeriod';
import {
  clearDevice,
  detectLocalAgent,
  rememberPairingCode,
  type LocalAgentStatus,
} from './localAgent';
import {
  startVerification,
  readVerification,
  verificationAction,
  type Verification,
} from './api/verifications';
import KeywordLibraryResults from './components/KeywordLibraryResults.vue';
import {
  ApiError,
  ImageInputClient,
  request,
  type ImageAsset,
  type InputRun,
  type ProductMaterials,
  type UnderstandingResult,
  type RootCandidates,
} from './api/images';

type InputSource = 'asin' | 'root' | 'image';
type WorkflowStatus = 'waiting' | 'running' | 'done' | 'review';
const sourceRevision = ref(0);
const syncRevision = ref(0);
const orchestration = ref<WorkflowView | null>(null);
const savedAction = ref<{ id: number; gate: string }>();
// 只有业务面板保存成功才发送门控事件；普通读取或刷新不能触发人工节点继续。
function businessSaved(gate: string) {
  savedAction.value = { id: (savedAction.value?.id ?? 0) + 1, gate };
  syncRevision.value++;
  sourceRevision.value++;
}
function profileSaved(): void {
  businessSaved('product_profile');
  void generateRootCandidates();
}
// 只接受当前研究的投影，刷新通知由各业务面板在草稿安全时自行消费。
function syncWorkflow(view: WorkflowView | null) {
  if (!view) {
    orchestration.value = null;
    return;
  }
  if (view.run_id === savedInputRun.value?.id) {
    const statusKey = (value: WorkflowView | null) => JSON.stringify(value && [
      value.workflow_id, value.status, value.current_node, value.nodes.map(node => [node.key, node.status]),
    ]);
    const changed = statusKey(orchestration.value) !== statusKey(view);
    orchestration.value = view;
    void refreshSummary(false, changed);
    if (view.nodes.some(node => node.key === 'root_selection' && ['running', 'waiting_human', 'waiting_external'].includes(node.status))) {
      void pollProductTermAnalysis(savedInputRun.value!);
    }
  }
}
const reportWeek = ref(latestReportWeek());
const recordsOpen = ref(false);
const historyPreview = new URLSearchParams(window.location.search).get('history') === '1';

type RootSuggestion = {
  id: string;
  text: string;
  translation: string;
  reason: string;
  selected: boolean;
};

// 当前只支持美国站；后续开放其他站点时在此追加即可。
const marketplaceOptions = [{ value: 'US', label: '美国站', flag: 'US' }];

const workflow = ref<WorkflowStatus[]>([
  'waiting',
  'waiting',
  'waiting',
  'waiting',
  'waiting',
  'waiting',
  'waiting',
  'waiting',
]);
// Display execution progress without granting business readiness before facts load.
const displayWorkflow = computed(() => {
  const result = [...workflow.value];
  const view = orchestration.value;
  if (!view || ['completed', 'aborted', 'superseded', 'incompatible'].includes(view.status))
    return result;
  const key = view.current_node ?? view.nodes.find((node) => node.status === 'failed')?.key;
  const index = stepForNode(key);
  if (index >= 0)
    result[index] = ['queued', 'running', 'waiting_external'].includes(view.status)
      ? 'running'
      : 'review';
  return result;
});
const selectedMarketplace = ref('');
const asinInput = ref('');
const rootInput = ref('');
const productInput = ref('');
const attachments = ref<File[]>([]);
const referencePriceInput = ref('');
const validReferencePrice = computed(
  () =>
    /^\d+(\.\d{1,4})?$/.test(referencePriceInput.value) && Number(referencePriceInput.value) > 0,
);
const selectedSources = computed<InputSource[]>(() => {
  const sources: InputSource[] = [];
  if (asinInput.value.trim()) sources.push('asin');
  if (rootInput.value.trim()) sources.push('root');
  if (attachments.value.length) sources.push('image');
  return sources;
});
const attachmentInput = ref<HTMLInputElement | null>(null);
const rootSuggestions = ref<RootSuggestion[]>([]);
const hasRootSuggestions = ref(false);
const rootCandidatesKnown = ref(false);
const productConfirmed = ref(false);
const productProfileConfirmed = ref(false);
const rootsConfirmed = ref(false);
const coreSelectionId = ref('');
const competitorSelectionId = ref('');
const runPending = ref(false);
const localAgent = ref<LocalAgentStatus>({
  state: 'offline',
  version: '',
  message: '正在检测这台电脑的紫鸟采集端',
});
const collectorMode = ref<'loading' | 'device' | 'server'>('loading');
let collectorInterval: number | undefined;
const pairingCode = ref('');
const pairingCodeBusy = ref(false);
const pairingCodeError = ref('');
const pairingCodeCopied = ref(false);
const notification = ref('');
const modelSettingsOpen = ref(false);
const notificationTimer = ref<number | undefined>();
const fileUrls = new Map<File, string>();

const selectedStep = ref(0);
const summaryReady = ref(false);
const summaryAvailable = ref(false);
const visitedSteps = ref<number[]>([]);
let summaryEpoch = 0;
let summaryPromise: Promise<void> = Promise.resolve();
let resultVersion = '';
const summaryError = ref('');
function mountStep(index: number): boolean {
  if (!savedInputRun.value) return index === 0;
  if (!summaryReady.value) return false;
  // Visited panels stay mounted to protect unsaved edits. If the summary is
  // unavailable, retain the existing read-only restoration as a fallback.
  return !summaryAvailable.value || selectedStep.value === index || visitedSteps.value.includes(index)
    || (orchestration.value?.status !== 'completed' && currentStepIndex.value === index);
}
watch(selectedStep, (index) => {
  if (summaryReady.value && !visitedSteps.value.includes(index))
    visitedSteps.value = [...visitedSteps.value, index];
});
async function refreshSummary(initial: boolean, graphChanged = false): Promise<void> {
  const run = savedInputRun.value;
  if (!run) return;
  const ticket = ++summaryEpoch;
  try {
    const summary = await readResultSummary(run);
    if (ticket !== summaryEpoch || savedInputRun.value?.id !== run.id) return;
    summaryError.value = '';
    summaryAvailable.value = true;
    if (initial) orchestration.value = summary.workflow;
    summary.steps.forEach((status, index) => {
      if (initial || !visitedSteps.value.includes(index)) workflow.value[index] = status;
    });
    coreSelectionId.value = summary.core_selection_id;
    competitorSelectionId.value = summary.competitor_selection_id;
    if (initial) {
      confirmedRootIds.value = summary.root_selection?.candidate_ids ?? [];
      rootsConfirmed.value = confirmedRootIds.value.length > 0;
      if (summary.report_week) reportWeek.value = summary.report_week;
      selectedStep.value = currentStep(summary.steps, summary.workflow);
      visitedSteps.value = [selectedStep.value];
    }
    if (!initial && (graphChanged || resultVersion !== summary.result_version)) syncRevision.value++;
    resultVersion = summary.result_version;
  } catch (e) {
    if (ticket === summaryEpoch && savedInputRun.value?.id === run.id) {
      summaryError.value = e instanceof Error ? e.message : '进度摘要读取失败';
      // Fall back to the existing read-only restoration; never invent completion.
      if (initial) summaryAvailable.value = false;
    }
  } finally {
    if (ticket === summaryEpoch && savedInputRun.value?.id === run.id) summaryReady.value = true;
  }
}

const followingProgress = ref(true);
const currentStepIndex = computed(() => currentStep(workflow.value, orchestration.value));
const currentInstruction = computed(() => {
  if (!savedInputRun.value) return '先选择站点，填写商品 ASIN 或上传图片，然后点击“保存商品”。';
  if (orchestration.value?.waiting_for?.instructions.trim())
    return orchestration.value.waiting_for.instructions;
  if (orchestration.value?.status === 'running')
    return '系统正在处理本步，可查看进度；有需要人工确认的内容时会暂停。';
  if (currentStepIndex.value === 0) {
    if (!understanding.value) return '商品输入已保存。点击“识别商品”，核对名称、类型和特征。';
    if (!productConfirmed.value && !rootCandidatesKnown.value)
      return '核对商品识别结果，在商品确认面板保存资料。';
    if (!productProfileConfirmed.value) return '请确认用途、款式结构、规格和可靠售价。';
    if (!hasRootSuggestions.value) return '四维基准确认后，系统会自动生成并分析候选商品词。';
    if (!productTermAnalysis.value) return '正在分析候选商品词的前30个自然商品。';
    if (['queued', 'running'].includes(productTermAnalysis.value.status)) return '正在采集并评分候选商品词，请查看下方真实进度。';
    if (!productTermAnalysis.value.best_candidate_ids.length) return '暂无达到门槛的商品词，请修改或重新生成准确品类词，再验证。';
    return '商品词相关度分析完成，系统已保存全部相关度大于 40% 的商品词。';
  }
  return workflowSteps[currentStepIndex.value]?.note ?? '查看关键词库发布结果。';
});
let followTimer: ReturnType<typeof setTimeout> | undefined;
watch(currentStepIndex, () => {
  clearTimeout(followTimer);
  followTimer = setTimeout(() => {
    if (followingProgress.value) selectedStep.value = currentStepIndex.value;
  }, 450);
});
// 用户主动翻看历史步骤后停止自动跟随，避免后台推进时把页面突然切走。
function navigateStep(index: number) {
  followingProgress.value = false;
  selectedStep.value = index;
  void nextTick(() => {
    const panel = document.getElementById('step-operation');
    panel?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    panel?.focus({ preventScroll: true });
  });
}
onBeforeUnmount(() => clearTimeout(followTimer));

provide('source-export-sync', computed(() => ({
  active: workflow.value[3] === 'running' || workflow.value[4] === 'running'
    || (!!orchestration.value && ['queued', 'running', 'waiting_external'].includes(orchestration.value.status)
      && stepForNode(orchestration.value.current_node) <= 4),
  revision: sourceRevision.value,
})));

const completedCount = computed(() => workflow.value.filter((status) => status === 'done').length);
const canSubmitInput = computed(
  () =>
    selectedMarketplace.value !== '' &&
    // 商品词仅作补充，必须提供 ASIN 或图片作为商品识别依据。
    ((isSourceSelected('asin') && sourceIsReady('asin')) ||
      (isSourceSelected('image') && sourceIsReady('image'))) &&
    selectedSources.value.every((source) => sourceIsReady(source)) &&
    (!attachments.value.length || !!asinInput.value.trim() || validReferencePrice.value) &&
    !runPending.value,
);
const selectedRootCount = computed(
  () => rootSuggestions.value.filter((root) => root.selected).length,
);
const imageClient = new ImageInputClient();
const restoring = ref(false);
const snapshotKey = 'keyword-library-active-run-v1';
const imageStatuses = ref(new Map<File, string>());
const uploadedImages = ref(new Map<File, ImageAsset>());
const imageUploadError = ref('');
const savedInputRun = ref<InputRun | null>(null);
const productMaterials = ref<ProductMaterials | null>(null);
const materialsError = ref('');
const understanding = ref<UnderstandingResult | null>(null);
const understandingError = ref('');
const identifying = ref(false);
const generatingRoots = ref(false);
const savingRoots = ref(false);
const rootError = ref('');
const productTermAnalysis = ref<ProductTermAnalysis | null>(null);
const candidateRecord = ref<RootCandidates | null>(null);
let candidateValidationRunId = '';
let analysisReading = false;
const evidenceRequests = new Set<string>();
const confirmedRootIds = ref<string[]>([]);
const verification = ref<Verification | null>(null);
const verifying = ref(false);
const verificationError = ref('');
const verificationActionBusy = ref(false);
const referenceImageId = ref('');
let verificationEpoch = 0;
let verificationTimer: ReturnType<typeof setTimeout> | undefined;
let analysisTimer: ReturnType<typeof setTimeout> | undefined;
onBeforeUnmount(() => {
  verificationEpoch++;
  clearTimeout(verificationTimer);
  clearTimeout(analysisTimer);
});
const referenceImages = computed(
  () => productMaterials.value?.images.flatMap((image) => (image.asset ? [image.asset] : [])) ?? [],
);
const selectedReferenceImage = computed(
  () => referenceImages.value.find((image) => image.id === referenceImageId.value) ?? null,
);
const selectedRootsVerified = computed(() => {
  const task = verification.value;
  const ids = rootSuggestions.value.filter((root) => root.selected).map((root) => root.id);
  return (
    !!task?.final &&
    task.reference_image_id === (referenceImageId.value || null) &&
    ids.length > 0 &&
    task.items.length === ids.length &&
    ids.every((id) => task.items.some((item) => item.candidate_id === id))
  );
});

async function pollProductTermAnalysis(run: InputRun): Promise<void> {
  if (analysisReading) return;
  clearTimeout(analysisTimer);
  analysisReading = true;
  try {
    const result = await readProductTermAnalysis(run);
    if (savedInputRun.value?.id !== run.id || !result) return;
    productTermAnalysis.value = result;
    if (['queued', 'running'].includes(result.status)) {
      analysisTimer = setTimeout(() => void pollProductTermAnalysis(run), 1500);
    } else {
      const selection = await imageClient.getRootSelection(run);
      if (savedInputRun.value?.id !== run.id) return;
      confirmedRootIds.value = selection?.candidate_ids ?? [];
      rootsConfirmed.value = confirmedRootIds.value.length > 0;
      workflow.value[0] = rootsConfirmed.value ? 'done' : 'review';
      if (candidateValidationRunId === run.id && result.best_candidate_ids.length) {
        candidateValidationRunId = '';
        businessSaved('product_term_analysis');
      }
    }
  } catch (error) {
    if (savedInputRun.value?.id === run.id)
      rootError.value = error instanceof Error ? error.message : '商品词分析进度读取失败';
    if (savedInputRun.value?.id === run.id) analysisTimer = setTimeout(() => void pollProductTermAnalysis(run), 5000);
  } finally {
    analysisReading = false;
  }
}

async function acceptCandidateVersion(result: RootCandidates): Promise<void> {
  const run = savedInputRun.value;
  if (!run || result.run_id !== run.id) return;
  candidateRecord.value = result;
  rootSuggestions.value = result.candidates.map(root => ({ ...root, selected: false }));
  confirmedRootIds.value = [];
  rootsConfirmed.value = false;
  productTermAnalysis.value = null;
  candidateValidationRunId = run.id;
  rootError.value = '';
  generatingRoots.value = true;
  try {
    const analysis = await startProductTermAnalysis(run);
    if (savedInputRun.value?.id !== run.id) return;
    productTermAnalysis.value = analysis;
    void pollProductTermAnalysis(run);
  } catch (error) {
    if (savedInputRun.value?.id === run.id) rootError.value = error instanceof Error ? error.message : '新候选验证启动失败，可点击继续验证';
  } finally { generatingRoots.value = false; }
}

async function loadProductTermEvidence(candidateId: string): Promise<void> {
  const run = savedInputRun.value;
  const analysis = productTermAnalysis.value;
  if (!run || !analysis) return;
  const key = `${analysis.id}:${candidateId}`;
  if (evidenceRequests.has(key)) return;
  evidenceRequests.add(key);
  try {
    const item = await request<ProductTermAnalysis['items'][number]>(`/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/product-term-analyses/${analysis.id}/candidates/${candidateId}`);
    if (savedInputRun.value?.id !== run.id || productTermAnalysis.value?.id !== analysis.id) return;
    productTermAnalysis.value.items = productTermAnalysis.value.items.map(current => current.candidate_id === candidateId ? item : current);
  } catch (error) {
    if (savedInputRun.value?.id === run.id) rootError.value = error instanceof Error ? error.message : '商品词证据读取失败，可收起后重试';
  } finally { evidenceRequests.delete(key); }
}

/** 后端按候选和主图幂等；断线后再次读取，绝不伪造已完成状态。 */
async function verifyAllRoots(resumeId?: string): Promise<void> {
  const run = savedInputRun.value;
  if (!run || verifying.value) return;
  const ids = rootSuggestions.value.filter((root) => root.selected).map((root) => root.id);
  if (!resumeId && ids.length === 0) return;
  const epoch = ++verificationEpoch;
  clearTimeout(verificationTimer);
  verifying.value = true;
  verificationError.value = '';
  const current = () => epoch === verificationEpoch && savedInputRun.value?.id === run.id;
  const accept = async (task: Verification): Promise<void> => {
    if (!current()) return;
    verification.value = task;
    if (
      task.status === 'queued' ||
      task.status === 'running' ||
      ['opening', 'open'].includes(task.review_status || '')
    ) {
      verifying.value = task.status === 'queued' || task.status === 'running';
      verificationTimer = setTimeout(() => {
        void readVerification(run, task.id).then(accept).catch(fail);
      }, 1500);
      return;
    }
    if (task.selection_saved) {
      const selection = await imageClient.getRootSelection(run);
      if (!current()) return;
      confirmedRootIds.value = selection?.candidate_ids ?? [];
      rootSuggestions.value.forEach((root) => {
        root.selected = confirmedRootIds.value.includes(root.id);
      });
      rootsConfirmed.value = confirmedRootIds.value.length > 0;
      workflow.value[0] = rootsConfirmed.value ? 'done' : 'review';
    }
    verifying.value = false;
  };
  const fail = (error: unknown) => {
    if (!current()) return;
    verifying.value = false;
    verificationError.value =
      error instanceof Error ? error.message : '搜索核验读取失败，请重新读取';
  };
  try {
    await accept(
      resumeId
        ? await readVerification(run, resumeId)
        : await startVerification(run, referenceImageId.value, ids),
    );
  } catch (error) {
    fail(error);
  }
}

/** 服务端先关闭人工窗口并保存会话，再继续未完成的采集。 */
async function actOnVerification(action: 'resume' | 'review'): Promise<void> {
  const run = savedInputRun.value;
  const task = verification.value;
  if (!run || !task || verificationActionBusy.value) return;
  verificationActionBusy.value = true;
  verificationError.value = '';
  try {
    const updated = await verificationAction(run, task.id, action);
    if (savedInputRun.value?.id !== run.id) return;
    verification.value = updated;
    await verifyAllRoots(updated.id);
  } catch (error) {
    verificationError.value = error instanceof Error ? error.message : '操作失败，请重试';
  } finally {
    verificationActionBusy.value = false;
  }
}

// 按选择集合推导确认状态，保存过程中改选也不会显示错误的成功状态。
watch(
  () => rootSuggestions.value.filter((root) => root.selected).map((root) => root.id),
  (ids) => {
    rootsConfirmed.value =
      ids.length > 0 &&
      ids.length === confirmedRootIds.value.length &&
      ids.every((id) => confirmedRootIds.value.includes(id));
    if (hasRootSuggestions.value) workflow.value[0] = rootsConfirmed.value ? 'done' : 'review';
  },
);
// 任务发生变化时撤下识别结果；旧请求返回后也不能覆盖新输入。
watch(rootsConfirmed, (confirmed) => {
  if (!confirmed) {
    workflow.value[1] = 'waiting';
    workflow.value[2] = 'waiting';
    workflow.value[3] = 'waiting';
    workflow.value[4] = 'waiting';
    workflow.value[5] = 'waiting';
    workflow.value[6] = 'waiting';
  }
});

watch(savedInputRun, () => {
  summaryEpoch++;
  summaryReady.value = false;
  summaryAvailable.value = false;
  visitedSteps.value = [];
  resultVersion = '';
  summaryError.value = '';
  selectedStep.value = 0;
  followingProgress.value = true;
  orchestration.value = null;
  coreSelectionId.value = '';
  competitorSelectionId.value = '';
  verificationEpoch++;
  clearTimeout(verificationTimer);
  verification.value = null;
  verifying.value = false;
  verificationError.value = '';
  referenceImageId.value = '';
  understanding.value = null;
  productConfirmed.value = false;
  productProfileConfirmed.value = false;
  clearTimeout(analysisTimer);
  productTermAnalysis.value = null;
  candidateRecord.value = null;
  candidateValidationRunId = '';
  understandingError.value = '';
  rootSuggestions.value = [];
  hasRootSuggestions.value = false;
  rootCandidatesKnown.value = false;
  confirmedRootIds.value = [];
  rootsConfirmed.value = false;
  rootError.value = '';
  workflow.value = workflowSteps.map(() => 'waiting');
  summaryPromise = savedInputRun.value ? refreshSummary(true) : Promise.resolve();
});

// Background synchronization reads facts; it never runs verification or a model.
const rootsDirty = computed(() => {
  const selected = rootSuggestions.value.filter((r) => r.selected).map((r) => r.id);
  return (
    selected.length !== confirmedRootIds.value.length ||
    selected.some((id) => !confirmedRootIds.value.includes(id))
  );
});
usePanelSync(
  () => syncRevision.value,
  () =>
    restoring.value ||
    identifying.value ||
    generatingRoots.value ||
    savingRoots.value ||
    verifying.value ||
    rootsDirty.value,
  async () => {
    const run = savedInputRun.value;
    if (!run) return;
    try {
      const [materials, product, roots, selection] = await Promise.all([
        imageClient.getMaterials(run),
        imageClient.getUnderstanding(run),
        imageClient.getRoots(run),
        imageClient.getRootSelection(run),
      ]);
      if (savedInputRun.value?.id !== run.id || rootsDirty.value) return;
      productMaterials.value = materials;
      understanding.value = product;
      confirmedRootIds.value = selection?.candidate_ids ?? [];
      if (roots) {
        candidateRecord.value = roots;
        rootCandidatesKnown.value = true;
        hasRootSuggestions.value = true;
        rootSuggestions.value = roots.candidates.map((root) => ({
          ...root,
          selected: confirmedRootIds.value.includes(root.id),
        }));
      }
      rootsConfirmed.value = confirmedRootIds.value.length > 0;
    } catch (error) {
      materialsError.value = error instanceof Error ? error.message : '后台结果同步失败';
    }
  },
);

/** 只保存任务引用；刷新仅 GET 已有结果，不重新生成或重新核验。 */
watch(
  [savedInputRun, referenceImageId, verification, reportWeek],
  () => {
    if (restoring.value) return;
    try {
      if (!savedInputRun.value) {
        localStorage.removeItem(snapshotKey);
        return;
      }
      localStorage.setItem(
        snapshotKey,
        JSON.stringify({
          run: savedInputRun.value,
          reportWeek: reportWeek.value,
          marketplace: selectedMarketplace.value,
          asin: asinInput.value,
          root: rootInput.value,
          product: productInput.value,
          referencePrice: referencePriceInput.value,
          sources: selectedSources.value,
          reference: referenceImageId.value,
          verificationId: verification.value?.id ?? '',
        }),
      );
    } catch {
      /* 浏览器禁用存储不影响当前任务。 */
    }
  },
  { flush: 'post' },
);

// 数据异步恢复后再定位第三步，避免浏览器在组件尚未挂载时忽略锚点。
function navigateFromHash() {
  if (window.location.hash === '#competitors-title') navigateStep(2);
}
onMounted(() => window.addEventListener('hashchange', navigateFromHash));
onBeforeUnmount(() => window.removeEventListener('hashchange', navigateFromHash));
watch(coreSelectionId, async (id) => {
  if (!id || window.location.hash !== '#competitors-title') return;
  navigateStep(2);
  await nextTick();
  document.getElementById('competitors-title')?.scrollIntoView({ block: 'start' });
});

async function refreshLocalAgent() {
  if (collectorMode.value === 'loading') {
    try {
      const result = await request<{ mode: 'server' | 'device' }>('/api/v1/browser-agent/mode');
      collectorMode.value = result.mode;
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) collectorMode.value = 'device';
      else return;
    }
  }
  if (collectorMode.value === 'server') {
    try {
      const result = await request<Pick<LocalAgentStatus, 'state' | 'version'>>(
        '/api/v1/browser-agent/status',
      );
      const labels = {
        online: '服务器采集器已连接',
        busy: '服务器采集器正在运行',
        attention_required: '服务器采集器需要处理',
        offline: '服务器采集器未连接',
      };
      localAgent.value = { ...result, message: labels[result.state] };
    } catch {
      localAgent.value = { state: 'offline', version: '', message: '无法读取服务器采集器状态' };
    }
    return;
  }
  localAgent.value = await detectLocalAgent();
}

async function generatePairingCode() {
  if (pairingCodeBusy.value) return;
  pairingCodeBusy.value = true;
  pairingCodeError.value = '';
  try {
    const result = await request<{ code: string }>('/api/v1/agent/pairing-codes', {
      method: 'POST',
    });
    if (!result.code) throw new Error('服务器没有返回配对码');
    clearDevice();
    rememberPairingCode(result.code);
    pairingCode.value = result.code;
    pairingCodeCopied.value = false;
    try {
      await navigator.clipboard.writeText(result.code);
      pairingCodeCopied.value = true;
    } catch {
      // Keep the code visible for manual entry when clipboard access is blocked.
    }
    await refreshLocalAgent();
  } catch (reason) {
    pairingCodeError.value = reason instanceof Error ? reason.message : '配对码生成失败';
  } finally {
    pairingCodeBusy.value = false;
  }
}

async function copyPairingCode() {
  if (!pairingCode.value) return;
  try {
    await navigator.clipboard.writeText(pairingCode.value);
    pairingCodeCopied.value = true;
    pairingCodeError.value = '';
  } catch {
    pairingCodeCopied.value = false;
    pairingCodeError.value = '剪贴板不可用，请手动选中配对码复制';
  }
}

onMounted(() => {
  void refreshLocalAgent();
  collectorInterval = window.setInterval(() => void refreshLocalAgent(), 15000);
});

onMounted(async () => {
  let snapshot;
  try {
    // 显式任务链接从服务端恢复，支持新标签页和没有本地缓存的浏览器。
    // 只读取已有记录，不能在恢复时重新采集或调用模型。
    const params = new URLSearchParams(window.location.search);
    // 显式新建入口不读取上次任务缓存；直到保存商品才创建新研究记录。
    if (params.get('new') === '1') return;
    const project = params.get('project');
    const runId = params.get('run');
    if (project || runId) {
      const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
      if (!project || !runId || !uuid.test(project) || !uuid.test(runId)) {
        throw new Error('任务链接不完整或格式不正确');
      }
      const saved = await request<
        InputRun & {
          input: {
            marketplace: string;
            asin: string | null;
            product_name: string | null;
            description: string;
            image_ids: string[];
            image_urls: string[];
            reference_price?: string | null;
          };
        }
      >(`/api/v1/projects/${project}/pipeline-runs/${runId}`);
      snapshot = {
        reportWeek: undefined,
        run: { id: saved.id, project_id: saved.project_id, status: saved.status },
        marketplace: saved.input.marketplace,
        asin: saved.input.asin || '',
        root: saved.input.product_name || '',
        product: saved.input.description,
        referencePrice: saved.input.reference_price ?? '',
        reference: saved.input.image_ids[0] || '',
      };
    } else {
      snapshot = JSON.parse(localStorage.getItem(snapshotKey) ?? 'null');
    }
  } catch (error) {
    materialsError.value = error instanceof Error ? error.message : '任务读取失败';
    return;
  }
  if (
    !snapshot ||
    typeof snapshot.run?.id !== 'string' ||
    typeof snapshot.run?.project_id !== 'string' ||
    !marketplaceOptions.some((option) => option.value === snapshot.marketplace)
  )
    return;
  restoring.value = true;
  try {
    selectedMarketplace.value = snapshot.marketplace;
    if (typeof snapshot.reportWeek === 'string') reportWeek.value = snapshot.reportWeek;
    asinInput.value = typeof snapshot.asin === 'string' ? snapshot.asin : '';
    rootInput.value = typeof snapshot.root === 'string' ? snapshot.root : '';
    productInput.value = typeof snapshot.product === 'string' ? snapshot.product : '';
    referencePriceInput.value =
      typeof snapshot.referencePrice === 'string' ? snapshot.referencePrice : '';
    await nextTick();
    savedInputRun.value = snapshot.run;
    await nextTick();
    referenceImageId.value = typeof snapshot.reference === 'string' ? snapshot.reference : '';
    const run = savedInputRun.value!;
    await summaryPromise;
    if (savedInputRun.value?.id !== run.id) return;
    const [materials, knownProduct, roots, selection] = await Promise.all([
      imageClient.getMaterials(run),
      imageClient.getUnderstanding(run),
      imageClient.getRoots(run),
      imageClient.getRootSelection(run),
    ]);
    if (savedInputRun.value?.id !== run.id) return;
    productMaterials.value = materials;
    understanding.value = knownProduct;
    confirmedRootIds.value = selection?.candidate_ids ?? [];
    rootsConfirmed.value = confirmedRootIds.value.length > 0;
    if (roots) {
      candidateRecord.value = roots;
      rootCandidatesKnown.value = true;
      if (mountStep(0)) productTermAnalysis.value = await readProductTermAnalysis(run);
      if (
        productTermAnalysis.value &&
        ['queued', 'running'].includes(productTermAnalysis.value.status)
      )
        void pollProductTermAnalysis(run);
      rootSuggestions.value = roots.candidates.map((root) => ({
        ...root,
        selected: confirmedRootIds.value.includes(root.id),
      }));
      hasRootSuggestions.value = true;
    }
    if (mountStep(0) && typeof snapshot.verificationId === 'string' && snapshot.verificationId) {
      await verifyAllRoots(snapshot.verificationId);
    }
  } catch (error) {
    materialsError.value = error instanceof Error ? error.message : '任务恢复失败，请重新读取';
  } finally {
    restoring.value = false;
  }
});


watch(() => summaryReady.value && mountStep(0) && savedInputRun.value?.id, async (id) => {
  if (!id || !savedInputRun.value || restoring.value) return;
  const run = savedInputRun.value;
  try {
    const value = await readProductTermAnalysis(run);
    if (savedInputRun.value?.id !== run.id) return;
    productTermAnalysis.value = value;
    if (value && ['queued', 'running'].includes(value.status)) void pollProductTermAnalysis(run);
  } catch (e) {
    if (savedInputRun.value?.id === run.id)
      materialsError.value = e instanceof Error ? e.message : '商品词结果读取失败';
  }
});

async function identifyProduct(): Promise<void> {
  const run = savedInputRun.value;
  if (!run || identifying.value) return;
  identifying.value = true;
  understandingError.value = '';
  try {
    const result = await imageClient.identify(run);
    if (savedInputRun.value?.id === run.id) {
      understanding.value = result;
      await loadProductMaterials();
    }
  } catch (error) {
    if (savedInputRun.value?.id === run.id) {
      understandingError.value = error instanceof Error ? error.message : '商品识别失败';
    }
  } finally {
    identifying.value = false;
  }
}

// 输入变化后撤下旧回执，避免把旧任务误认为当前输入已保存。
watch(
  [selectedMarketplace, asinInput, rootInput, productInput, referencePriceInput, selectedSources],
  () => {
    if (restoring.value) return;
    savedInputRun.value = null;
    productMaterials.value = null;
    materialsError.value = '';
  },
);
watch(selectedMarketplace, () => {
  imageStatuses.value.clear();
  uploadedImages.value.clear();
});

/** 使用点击瞬间的输入快照；提交 ASIN 或图片后保存任务并查询统一资料。 */
async function saveProductInput(): Promise<void> {
  const files = isSourceSelected('image') ? [...attachments.value] : [];
  const marketplace = selectedMarketplace.value;
  const asin = isSourceSelected('asin') ? asinInput.value.trim().toUpperCase() : '';
  // 图片不能提供可靠售价；纯图片新研究必须由用户给出基准，已有历史输入仍可读取。
  if (
    (files.length && !asin && !validReferencePrice.value) ||
    (referencePriceInput.value && !validReferencePrice.value)
  ) {
    imageUploadError.value = '请填写大于 0 的本品参考售价（USD，最多 4 位小数）';
    return;
  }
  const referencePrice = referencePriceInput.value;
  if (asin && !/^[A-Z0-9]{10}$/.test(asin)) {
    imageUploadError.value = 'ASIN 必须是 10 位字母或数字';
    return;
  }
  imageUploadError.value = '';
  savedInputRun.value = null;
  productMaterials.value = null;
  materialsError.value = '';
  runPending.value = true;
  // 真实输入提交时清除旧演示状态，防止把模拟进度误认为后台执行结果。
  hasRootSuggestions.value = false;
  rootsConfirmed.value = false;
  workflow.value = workflowSteps.map(() => 'waiting');
  try {
    const description = [
      productInput.value,
      isSourceSelected('root') ? `已知词：${rootInput.value}` : '',
    ]
      .filter(Boolean)
      .join('\n');
    savedInputRun.value = await imageClient.submit(
      marketplace,
      files,
      asin,
      description,
      (file, status, asset) => {
        imageStatuses.value.set(file, status);
        if (asset) uploadedImages.value.set(file, asset);
      },
      referencePrice,
    );
    setNotification('商品输入已保存');
    // 保存后退出 new=1 空白模式，刷新应恢复刚创建的研究，而不是再次打开空白页。
    window.history.replaceState(
      null,
      '',
      `/?${new URLSearchParams({
        project: savedInputRun.value.project_id,
        run: savedInputRun.value.id,
      })}`,
    );
    // 输入任务已经保存，资料读取失败时保留编号，用户可单独重试读取。
    await loadProductMaterials();
  } catch (error) {
    imageUploadError.value = error instanceof Error ? error.message : '输入保存失败，请稍后重试';
  } finally {
    runPending.value = false;
  }
}

/** 查询资料失败不回滚保存成功的回执，也不再次上传图片。 */
async function loadProductMaterials(): Promise<void> {
  const run = savedInputRun.value;
  if (!run) return;
  materialsError.value = '';
  try {
    const materials = await imageClient.getMaterials(run);
    // 读取期间用户可能修改输入，只展示仍属于当前回执的资料。
    if (savedInputRun.value?.id === run.id) productMaterials.value = materials;
  } catch (error) {
    if (savedInputRun.value?.id === run.id) {
      materialsError.value = error instanceof Error ? error.message : '资料读取失败';
    }
  }
}

/** 前端提前反馈格式与大小；文件真实内容仍由后端校验。 */
function addImages(files: File[]): void {
  if (runPending.value) return;
  const valid = files.filter(
    (file) =>
      ['image/jpeg', 'image/png', 'image/webp'].includes(file.type) &&
      file.size > 0 &&
      file.size <= 10 * 1024 * 1024,
  );
  const slots = Math.max(0, 5 - attachments.value.length);
  const accepted = valid.slice(0, slots);
  for (const file of accepted) fileUrls.set(file, URL.createObjectURL(file));
  attachments.value = [...attachments.value, ...accepted];
  savedInputRun.value = null;
  productMaterials.value = null;
  materialsError.value = '';
  imageUploadError.value = '';
  if (valid.length !== files.length) {
    imageUploadError.value = '仅支持非空 JPG、PNG、WebP，每张最多 10 MB';
  } else if (valid.length > slots) {
    imageUploadError.value = '每次最多添加 5 张图片';
  }
}

function setNotification(message: string): void {
  notification.value = message;
  if (notificationTimer.value) window.clearTimeout(notificationTimer.value);
  notificationTimer.value = window.setTimeout(() => {
    notification.value = '';
  }, 3200);
}

function openFilePicker(): void {
  attachmentInput.value?.click();
}

function isSourceSelected(source: InputSource): boolean {
  return selectedSources.value.includes(source);
}

function sourceIsReady(source: InputSource): boolean {
  if (source === 'asin') return /^[A-Z0-9]{10}$/.test(asinInput.value.trim().toUpperCase());
  if (source === 'root') return rootInput.value.trim().length > 0;
  return attachments.value.some((file) => file.type.startsWith('image/'));
}

function onFilesSelected(event: Event): void {
  const input = event.target as HTMLInputElement;
  addImages(Array.from(input.files ?? []));
  input.value = '';
}
function removeAttachment(index: number): void {
  if (runPending.value) return;
  savedInputRun.value = null;
  productMaterials.value = null;
  materialsError.value = '';
  const file = attachments.value[index];
  if (file) {
    const url = fileUrls.get(file);
    if (url) URL.revokeObjectURL(url);
    fileUrls.delete(file);
  }
  attachments.value = attachments.value.filter((_, fileIndex) => fileIndex !== index);
}

function attachmentUrl(file: File): string | undefined {
  return uploadedImages.value.get(file)?.content_url ?? fileUrls.get(file);
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function submitProductInput(): void {
  if (!selectedMarketplace.value) {
    setNotification('请先选择站点');
    return;
  }
  if (!isSourceSelected('asin') && !isSourceSelected('image')) {
    setNotification('请提供 ASIN 或图片，至少填写一项');
    return;
  }
  if (!selectedSources.value.every((source) => sourceIsReady(source))) {
    setNotification('请补全已选择的输入项');
    return;
  }
  // ASIN、图片及组合输入统一保存真实任务；识别与推荐由用户分别触发。
  void saveProductInput();
}

/** 显式调用 MIMO，重复生成由后端复用缓存；旧任务响应不能污染新输入。 */
async function generateRootCandidates(): Promise<void> {
  const run = savedInputRun.value;
  if (
    !run ||
    !understanding.value ||
    generatingRoots.value ||
    (!productConfirmed.value && !rootCandidatesKnown.value) ||
    (!productProfileConfirmed.value && !rootCandidatesKnown.value)
  )
    return;
  generatingRoots.value = true;
  rootError.value = '';
  try {
    const result = await imageClient.generateRoots(run);
    let selection = await imageClient.getRootSelection(run);
    if (savedInputRun.value?.id !== run.id) return;
    candidateRecord.value = result;
    confirmedRootIds.value = selection?.candidate_ids ?? [];
    rootSuggestions.value = result.candidates.map((root) => ({
      ...root,
      selected: confirmedRootIds.value.includes(root.id),
    }));
    hasRootSuggestions.value = true;
    rootCandidatesKnown.value = true;
    productTermAnalysis.value = await startProductTermAnalysis(run);
    if (['queued', 'running'].includes(productTermAnalysis.value.status))
      void pollProductTermAnalysis(run);
    selection = await imageClient.getRootSelection(run);
    confirmedRootIds.value = selection?.candidate_ids ?? [];
    rootSuggestions.value.forEach((root) => {
      root.selected = confirmedRootIds.value.includes(root.id);
    });
    rootsConfirmed.value = confirmedRootIds.value.length > 0;
    workflow.value[0] = rootsConfirmed.value ? 'done' : 'review';
  } catch (error) {
    if (savedInputRun.value?.id === run.id)
      rootError.value = error instanceof Error ? error.message : '商品词生成或分析失败';
  } finally {
    generatingRoots.value = false;
  }
}

async function confirmRoots(): Promise<void> {
  const run = savedInputRun.value;
  const ids = rootSuggestions.value.filter((root) => root.selected).map((root) => root.id);
  if (!run || ids.length === 0 || savingRoots.value || !selectedRootsVerified.value) return;
  savingRoots.value = true;
  rootError.value = '';
  try {
    const result = await imageClient.confirmRoots(run, ids);
    if (savedInputRun.value?.id !== run.id) return;
    confirmedRootIds.value = result.candidate_ids;
    rootsConfirmed.value = true;
    workflow.value[0] = 'done';
    setNotification(`已人工保存 ${result.candidate_ids.length} 个商品词`);
    businessSaved('root_selection');
  } catch (error) {
    if (savedInputRun.value?.id === run.id)
      rootError.value = error instanceof Error ? error.message : '商品词保存失败';
  } finally {
    savingRoots.value = false;
  }
}

async function overrideProductTerms(ids: string[]): Promise<void> {
  const run = savedInputRun.value;
  if (!run || !ids.length) return;
  try {
    const selection = await imageClient.confirmRoots(run, ids);
    confirmedRootIds.value = selection.candidate_ids;
    rootsConfirmed.value = true;
    businessSaved('root_selection');
    setNotification(`已人工保存 ${ids.length} 个商品词`);
  } catch (error) {
    rootError.value = error instanceof Error ? error.message : '商品词保存失败';
  }
}

/** 显式强制刷新：忽略缓存结果，重新采集并对全部候选重新打分。 */
async function reanalyzeProductTerms(): Promise<void> {
  const run = savedInputRun.value;
  if (!run || generatingRoots.value) return;
  generatingRoots.value = true;
  rootError.value = '';
  candidateValidationRunId = run.id;
  try {
    // 空结果整批重算；中断结果只续跑未完成候选，避免重复计费。
    const analysis = productTermAnalysis.value;
    const refresh =
      !!analysis &&
      (analysis.status === 'failed' || (analysis.complete && !analysis.best_candidate_ids.length));
    const result = await startProductTermAnalysis(run, refresh);
    if (savedInputRun.value?.id !== run.id) return;
    productTermAnalysis.value = result;
    if (['queued', 'running'].includes(result.status)) void pollProductTermAnalysis(run);
  } catch (error) {
    if (savedInputRun.value?.id === run.id)
      rootError.value = error instanceof Error ? error.message : '商品词重新分析失败';
  } finally {
    generatingRoots.value = false;
  }
}

function resetWorkflow(): void {
  runPending.value = false;
  rootsConfirmed.value = false;
  hasRootSuggestions.value = false;
  rootSuggestions.value = [];
  workflow.value = workflowSteps.map(() => 'waiting');
  confirmedRootIds.value = [];
  rootError.value = '';
  setNotification('已收起商品词结果，可重新读取候选');
}

function onDrop(event: DragEvent): void {
  event.preventDefault();
  addImages(Array.from(event.dataTransfer?.files ?? []));
}
onBeforeUnmount(() => {
  if (notificationTimer.value) window.clearTimeout(notificationTimer.value);
  if (collectorInterval !== undefined) window.clearInterval(collectorInterval);
  for (const url of fileUrls.values()) URL.revokeObjectURL(url);
});
</script>

<template>
  <div v-if="collectorMode !== 'loading'" class="local-agent" :data-state="localAgent.state">
    <span>{{ localAgent.message }}</span>
    <small v-if="localAgent.version">v{{ localAgent.version }}</small>
    <button
      v-if="localAgent.state === 'attention_required'"
      type="button"
      class="banner-recheck"
      style="
        margin-left: auto;
        padding: 3px 10px;
        border-radius: 6px;
        border: 1px solid #cbd5e1;
        background: #fff;
        color: #334155;
        text-decoration: none;
        font-size: 12px;
        cursor: pointer;
      "
      @click="refreshLocalAgent()"
    >
      重新检测
    </button>
    <a
      v-if="collectorMode === 'device'"
      class="banner-download"
      href="/agent/KeywordLibraryAgent.zip"
      download="KeywordLibraryAgent.zip"
      style="
        padding: 3px 10px;
        border-radius: 6px;
        background: #0f8f83;
        color: #fff;
        text-decoration: none;
        font-size: 12px;
      "
      >下载/重装采集器</a
    >
    <div v-if="collectorMode === 'device'" class="pairing-tools" aria-label="采集器配对">
      <button type="button" :disabled="pairingCodeBusy" @click="generatePairingCode">
        {{ pairingCodeBusy ? '正在生成…' : pairingCode ? '重新生成配对码' : '生成配对码' }}
      </button>
      <code v-if="pairingCode" class="pairing-code" aria-live="polite">{{ pairingCode }}</code>
      <button
        v-if="pairingCode"
        type="button"
        class="pairing-copy"
        aria-label="复制配对码"
        title="复制配对码"
        @click="copyPairingCode"
      >
        <Copy :size="14" />
      </button>
      <small v-if="pairingCode">
        {{ pairingCodeCopied ? '已复制' : '请手动复制' }}；在采集器窗口输入，10 分钟内有效
      </small>
    </div>
    <span v-if="collectorMode === 'device' && pairingCodeError" class="pairing-error" role="alert">{{ pairingCodeError }}</span>
  </div>
  <div class="app-shell guided-app">
    <main id="main" class="main-content">
      <header class="topbar">
        <div class="breadcrumbs">
          <Layers3 :size="18" /><strong>关键词库工作台</strong><ChevronRight :size="14" /><span>{{
            workflowSteps[selectedStep]?.title
          }}</span>
        </div>
        <div class="topbar-actions">
          <a class="quiet-button" href="/?new=1">＋ 新建研究</a>
          <button type="button" class="quiet-button" @click="recordsOpen = true">
            <FolderOpen :size="16" />研究记录
          </button>
          <button type="button" class="quiet-button" @click="modelSettingsOpen = true">
            大模型
          </button>
          <span class="demo-indicator"><span></span>{{ completedCount }} / 8 步完成</span>
          <button class="icon-button help-top" aria-label="帮助" title="帮助">
            <CircleHelp :size="18" />
          </button>
        </div>
      </header>

      <ModelSettings v-if="modelSettingsOpen" @close="modelSettingsOpen = false" />
      <ResearchRecords
        v-if="recordsOpen"
        :current-run="savedInputRun?.id"
        @close="recordsOpen = false"
      />
      <div class="content-wrap">
        <section class="page-heading">
          <div>
            <div class="eyebrow"><span class="eyebrow-dot"></span> AMAZON KEYWORD WORKSPACE</div>
            <h1>关键词库搭建</h1>
            <p>
              {{ selectedMarketplace || '待选站点'
              }}<template v-if="asinInput"> · {{ asinInput }}</template> ·
              按步骤完成研究，需要人工确认时系统会提示。
            </p>
          </div>
          <button
            v-if="selectedStep === 0 && (hasRootSuggestions || rootsConfirmed)"
            class="quiet-button"
            type="button"
            :disabled="savingRoots || generatingRoots"
            @click="resetWorkflow"
          >
            <Activity :size="15" />收起商品词结果
          </button>
        </section>

        <WorkflowWorkspace
          v-model="selectedStep"
          :current="currentStepIndex"
          :statuses="displayWorkflow"
          :facts="workflow"
          :instruction="currentInstruction"
          :run-ready="!!savedInputRun"
          :error="orchestration?.error"
          @navigate="navigateStep"
        >
          <template #task-input>
            <ConfirmedProductTerms
              v-if="selectedStep === 1 && savedInputRun"
              :run="savedInputRun"
              @goto="navigateStep"
            />
            <ConfirmedCoreTerms
              v-if="selectedStep === 2 && savedInputRun"
              :run="savedInputRun"
              @goto="navigateStep"
            />
          </template>
          <template #step-input>
            <ConfirmedCoreTerms
              v-if="selectedStep === 2 && savedInputRun"
              :run="savedInputRun"
              @goto="navigateStep"
            />
          </template>
          <template #controls>
            <section v-if="savedInputRun" class="report-period" aria-label="报告周期">
              <h2>本次研究周期</h2>
              <label>ABA 周结束日（已结束的周六）<input v-model="reportWeek" type="date" /></label>
              <details>
                <summary>查看两路数据时间口径</summary>
                <p>
                  ABA 使用所选报告周；卖家精灵固定使用最近 30
                  天。两个来源仅合并关键词文本，排名、份额等指标保留各自口径。
                </p>
              </details>
            </section>
            <WorkflowBar
              v-if="savedInputRun && summaryReady"
              :key="`workflow-${savedInputRun.id}`"
              v-model:report-week="reportWeek"
              :run="savedInputRun"
              :saved-action="savedAction"
              :history-preview="historyPreview"
              :term-analysis="productTermAnalysis"
              @view="syncWorkflow"
            />
          </template>

          <p v-if="savedInputRun && !summaryReady" role="status">正在读取已保存的进度…</p>
          <p v-if="summaryError" role="alert">{{ summaryError }}</p>
          <div v-if="mountStep(0)" v-show="selectedStep === 0" class="workspace-pane" data-step-pane="0">
            <section class="input-panel input-panel--compact" aria-labelledby="input-title">
              <div class="product-entry-heading">
                <span class="product-entry-icon"
                  ><FolderOpen :size="19" :stroke-width="1.7"
                /></span>
                <div>
                  <h2 id="input-title">1. 添加研究商品</h2>
                  <p>从一个 ASIN 或一张商品图片开始</p>
                </div>
              </div>

              <div class="product-entry-row">
                <div class="product-identifier">
                  <label class="product-marketplace">
                    <select
                      v-model="selectedMarketplace"
                      :disabled="runPending"
                      required
                      aria-label="选择 Amazon 站点"
                    >
                      <option value="" disabled>选择站点</option>
                      <option
                        v-for="option in marketplaceOptions"
                        :key="option.value"
                        :value="option.value"
                      >
                        {{ option.label }} ({{ option.flag }})
                      </option>
                    </select>
                    <ChevronDown :size="14" />
                  </label>
                  <span class="product-field-divider"></span>
                  <input
                    v-model="asinInput"
                    aria-label="商品 ASIN"
                    :disabled="runPending"
                    type="text"
                    maxlength="10"
                    placeholder="输入商品 ASIN，例如 B0XXXXXXXX"
                  />
                </div>
                <span class="product-entry-or">或</span>
                <div class="product-image-entry" @dragover.prevent @drop="onDrop">
                  <button
                    class="product-image-button"
                    :disabled="runPending"
                    type="button"
                    title="支持拖入 JPG、PNG、WebP，最多 5 张，每张 10 MB"
                    @click="openFilePicker"
                  >
                    <ImagePlus :size="17" />上传图片
                  </button>
                  <input
                    ref="attachmentInput"
                    class="visually-hidden"
                    type="file"
                    accept="image/jpeg,image/png,image/webp"
                    multiple
                    @change="onFilesSelected"
                  />
                </div>
              </div>

              <div
                v-if="isSourceSelected('image') && attachments.length"
                class="attachment-list"
                aria-label="已添加图片"
              >
                <div
                  v-for="(file, index) in attachments"
                  :key="`${file.name}-${file.lastModified}`"
                  class="attachment-item"
                >
                  <img v-if="attachmentUrl(file)" :src="attachmentUrl(file)" :alt="file.name" />
                  <span v-else class="attachment-file-icon"><FileImage :size="16" /></span>
                  <span class="attachment-copy"
                    ><strong>{{ file.name }}</strong
                    ><small
                      >{{ formatFileSize(file.size) }} ·
                      {{ imageStatuses.get(file) ?? '待上传' }}</small
                    ></span
                  >
                  <button
                    class="icon-button attachment-remove"
                    :disabled="runPending"
                    :aria-label="`移除 ${file.name}`"
                    @click="removeAttachment(index)"
                  >
                    <X :size="15" />
                  </button>
                </div>
              </div>

              <div
                v-if="
                  asinInput.trim() ||
                  attachments.length ||
                  referencePriceInput ||
                  productMaterials?.reference_price
                "
                class="reference-price-input"
              >
                <label for="product-reference-price"
                  >本品参考售价（USD）<span v-if="!asinInput.trim()"> · 纯图片必填</span></label
                >
                <input
                  id="product-reference-price"
                  v-model="referencePriceInput"
                  type="number"
                  min="0.0001"
                  step="0.0001"
                  inputmode="decimal"
                  placeholder="例如：20.00"
                  :disabled="runPending || restoring"
                  :required="!asinInput.trim()"
                />
                <p>
                  ASIN 会先读取 Amazon
                  当前价；只有页面价格缺失、区间价或优惠价时才需要填写。纯图片必须填写当前这款规格、包装数量的整套销售价格，不是成本价。
                </p>
                <p v-if="validReferencePrice">
                  ±15% 参考区间：{{ (Number(referencePriceInput) * 0.85).toFixed(2) }}～{{
                    (Number(referencePriceInput) * 1.15).toFixed(2)
                  }}
                  USD
                </p>
              </div>
              <p v-if="imageUploadError" role="alert" class="upload-error">
                {{ imageUploadError }}
              </p>
              <div v-if="savedInputRun" class="upload-receipt product-workspace">
                <div class="saved-input-status">✓ 商品输入已保存</div>
                <details class="input-source-details">
                  <summary>商品来源与输入记录</summary>
                  <span>任务编号：{{ savedInputRun.id }}</span>
                  <template v-if="productMaterials">
                    <span>商品资料已整理 · {{ productMaterials.marketplace }} 站</span>
                    <span v-if="productMaterials.asin">ASIN：{{ productMaterials.asin }}</span>
                    <span>已关联图片：{{ productMaterials.images.length }} 张</span>
                    <span v-if="productMaterials.reference_price"
                      >用户提供参考售价：{{ productMaterials.reference_price }}
                      {{ productMaterials.reference_currency }}</span
                    >
                    <span v-if="productMaterials.asin_resolution === 'awaiting_provider'">
                      ASIN 商品资料：点击识别时自动读取
                    </span>
                    <template v-if="productMaterials.asin_product">
                      <strong>Amazon 商品标题：{{ productMaterials.asin_product.title }}</strong>
                      <a
                        :href="productMaterials.asin_product.source_url"
                        target="_blank"
                        rel="noopener noreferrer"
                        >查看商品详情来源</a
                      >
                      <a
                        v-if="productMaterials.asin_product.image_url"
                        :href="productMaterials.asin_product.image_url"
                        target="_blank"
                        rel="noopener noreferrer"
                        >查看商品主图</a
                      >
                    </template>
                    <span v-for="warning in productMaterials.warnings" :key="warning">{{
                      warning
                    }}</span>
                  </template>
                </details>
                <span v-if="materialsError" role="alert"
                  >输入已保存，资料读取失败：{{ materialsError }}</span
                >
                <button
                  v-if="materialsError"
                  class="text-button"
                  type="button"
                  @click="loadProductMaterials"
                >
                  重试读取资料
                </button>
                <button
                  v-if="!understanding"
                  class="primary-button"
                  type="button"
                  :disabled="identifying || runPending"
                  @click="identifyProduct"
                >
                  <LoaderCircle v-if="identifying" :size="16" class="spin" />
                  {{ identifying ? 'Qwen 3.8 Max 正在识别…' : '识别商品 · Qwen 3.8 Max' }}
                </button>
                <span v-if="!understanding"
                  >点击识别后，ASIN 输入会先读取商品详情；商品资料将发送至 MIMO
                  进行识别，产生模型调用费用。</span
                >
                <span v-if="understandingError" role="alert" class="upload-error">{{
                  understandingError
                }}</span>
                <ProductConfirmationPanel
                  v-if="understanding && savedInputRun"
                  :key="`product-confirm-${savedInputRun.id}`"
                  :run="savedInputRun"
                  :sync-revision="syncRevision"
                  :product="understanding.product"
                  :locked="generatingRoots"
                  @confirmed="productConfirmed = $event"
                  @saved="businessSaved('product_confirmation')"
                />
                <ProductProfilePanel
                  v-if="understanding && savedInputRun"
                  :key="`product-profile-${savedInputRun.id}`"
                  :run="savedInputRun"
                  :enabled="productConfirmed || rootCandidatesKnown"
                  :locked="rootCandidatesKnown || generatingRoots"
                  @confirmed="productProfileConfirmed = $event"
                  @saved="profileSaved"
                />
                <details
                  v-if="understanding"
                  class="understanding-result"
                  aria-label="商品识别结果"
                >
                  <summary>查看模型原始识别结果</summary>
                  <strong>商品识别结果 · {{ understanding.model }}</strong>
                  <p>商品名称：{{ understanding.product.product_name }}</p>
                  <p>商品类型：{{ understanding.product.product_type }}</p>
                  <strong>识别到的特征</strong>
                  <ul v-if="understanding.product.features.length">
                    <li v-for="feature in understanding.product.features" :key="feature">
                      {{ feature }}
                    </li>
                  </ul>
                  <p v-else>暂无可确认特征</p>
                  <strong>待确认信息</strong>
                  <ul v-if="understanding.product.uncertainties.length">
                    <li v-for="item in understanding.product.uncertainties" :key="item">
                      {{ item }}
                    </li>
                  </ul>
                  <p v-else>模型未列出待确认项，请仍核对结果。</p>
                  <p v-for="warning in understanding.warnings" :key="warning">{{ warning }}</p>
                </details>
                <div v-if="understanding && !productMaterials?.asin" class="reference-picker">
                  <label v-if="!productMaterials?.asin" for="reference-image">搜索核验主图</label>
                  <select
                    v-if="!productMaterials?.asin"
                    id="reference-image"
                    v-model="referenceImageId"
                    :disabled="verifying"
                  >
                    <option value="">请选择主图，不默认使用场景图</option>
                    <option v-for="image in referenceImages" :key="image.id" :value="image.id">
                      {{ image.original_name }}
                    </option>
                  </select>
                  <figure v-if="selectedReferenceImage" class="reference-image-preview">
                    <img
                      :src="selectedReferenceImage.content_url"
                      :alt="`当前搜索核验主图：${selectedReferenceImage.original_name}`"
                    />
                    <figcaption>{{ selectedReferenceImage.original_name }}</figcaption>
                  </figure>
                  <small>选择清晰商品图作为补充证据；商品词分析会采集最多30个自然商品。</small>
                </div>
                <button
                  v-if="understanding && !hasRootSuggestions"
                  class="primary-button"
                  type="button"
                  :disabled="
                    generatingRoots ||
                    (!productConfirmed && !rootCandidatesKnown) ||
                    (!productProfileConfirmed && !rootCandidatesKnown) ||
                    (!productMaterials?.asin && !referenceImageId)
                  "
                  @click="generateRootCandidates"
                >
                  <LoaderCircle v-if="generatingRoots" :size="16" class="spin" />
                  {{
                    generatingRoots ? '正在生成并分析商品词…' : '生成并分析商品词 · Qwen 3.8 Max'
                  }}
                </button>
                <span v-if="rootError" role="alert" class="upload-error">{{ rootError }}</span>
                <ProductTermCandidatesPanel
                  v-if="candidateRecord && savedInputRun"
                  :key="savedInputRun.id"
                  :run="savedInputRun"
                  :candidates="candidateRecord"
                  :locked="generatingRoots || ['queued', 'running'].includes(productTermAnalysis?.status ?? '') || orchestration?.status === 'running'"
                  @updated="acceptCandidateVersion"
                />
                <button v-if="candidateRecord && !productTermAnalysis" type="button" :disabled="generatingRoots" @click="reanalyzeProductTerms">验证当前候选</button>
                <ProductTermAnalysisPanel
                  v-if="productTermAnalysis"
                  :analysis="productTermAnalysis"
                  :busy="generatingRoots"
                  @override="overrideProductTerms"
                  @reanalyze="reanalyzeProductTerms"
                  @evidence="loadProductTermEvidence"
                />
              </div>

              <div class="product-entry-actions">
                <details class="input-extras">
                  <summary>补充信息 <span>选填</span></summary>
                  <div class="input-extras-fields">
                    <label class="source-input-field source-input-field--root">
                      <span>商品词或核心词 <small>选填</small></span>
                      <textarea
                        v-model="rootInput"
                        aria-label="商品词或核心词"
                        :disabled="runPending"
                        maxlength="800"
                        placeholder="例如 dumbbell rack、home gym；多个词可换行输入"
                      ></textarea>
                    </label>

                    <label class="product-description">
                      <span>补充商品名称或使用场景 <small>选填</small></span>
                      <textarea
                        v-model="productInput"
                        :disabled="runPending"
                        :maxlength="800"
                        placeholder="例如：面向家庭健身用户，商品强调稳定、节省空间和易组装。"
                        aria-label="补充商品名称或使用场景"
                      ></textarea>
                    </label>
                  </div>
                </details>

                <div class="input-panel-footer">
                  <button
                    class="primary-button"
                    type="button"
                    :disabled="!canSubmitInput"
                    @click="submitProductInput"
                  >
                    <LoaderCircle v-if="runPending" :size="16" class="spin" />
                    <Sparkles v-else :size="16" />
                    {{ runPending ? '正在保存…' : '保存商品' }}
                    <ArrowRight v-if="!runPending" :size="15" />
                  </button>
                </div>
              </div>
            </section>

            <section
              v-if="hasRootSuggestions && !productTermAnalysis"
              class="roots-panel"
              aria-labelledby="roots-title"
            >
              <div class="section-header roots-header">
                <div>
                  <div class="panel-kicker">
                    <span class="step-marker step-marker--amber">AI</span><span>商品识别结果</span>
                  </div>
                  <h2 id="roots-title">
                    候选商品词 <span class="count-text">{{ selectedRootCount }} 个</span>
                  </h2>
                  <p>候选生成后会自动搜索并分析，无需手动勾选启动。</p>
                </div>
                <button
                  class="text-button"
                  type="button"
                  :disabled="savingRoots || verifying"
                  @click="rootSuggestions.forEach((root) => (root.selected = true))"
                >
                  全选
                </button>
              </div>
              <div class="root-list">
                <div v-for="root in rootSuggestions" :key="root.id" class="root-row">
                  <input
                    :id="`root-${root.id}`"
                    v-model="root.selected"
                    type="checkbox"
                    :disabled="savingRoots || verifying"
                  />
                  <label class="root-check" :for="`root-${root.id}`"><Check :size="12" /></label>
                  <label class="root-term" :for="`root-${root.id}`"
                    ><strong>{{ root.text }}</strong
                    ><small>{{ root.translation }}</small></label
                  >
                  <span class="root-reason">{{ root.reason }}</span>
                </div>
              </div>
              <div class="reference-picker" style="padding: 0 24px 16px">
                <button
                  type="button"
                  class="text-button"
                  :disabled="verifying || savingRoots || !selectedRootCount"
                  @click="verifyAllRoots()"
                >
                  {{ verifying ? '正在验证已选词根…' : `验证已选词根（${selectedRootCount}）` }}
                </button>
                <p v-if="verificationError" role="alert" class="upload-error">
                  {{ verificationError }}
                </p>
                <RootVerificationPanel
                  v-if="verification"
                  :task="verification"
                  :busy="verificationActionBusy"
                  @resume="actOnVerification('resume')"
                  @review="actOnVerification('review')"
                />
              </div>
              <div class="roots-footer">
                <span><Info :size="14" />验证只提供参考，最终选择由你保存</span>
                <button
                  class="primary-button primary-button--small"
                  type="button"
                  :disabled="
                    savingRoots ||
                    verifying ||
                    !selectedRootCount ||
                    rootsConfirmed ||
                    !selectedRootsVerified
                  "
                  @click="confirmRoots"
                >
                  <CheckCircle2 :size="15" />{{
                    savingRoots ? '保存中…' : rootsConfirmed ? '词根选择已保存' : '保存词根选择'
                  }}
                </button>
              </div>
            </section>
          </div>
          <div v-show="selectedStep === 1" class="workspace-pane" data-step-pane="1">
            <p v-if="!savedInputRun || !rootsConfirmed" class="workspace-empty">
              先在第一步确认商品四维基准并完成商品词相关度分析，随后在这里继续操作。
            </p>
            <CoreTermsPanel
              v-if="savedInputRun && rootsConfirmed && mountStep(1)"
              :key="savedInputRun.id"
              v-model:report-week="reportWeek"
              :run="savedInputRun"
              :sync-revision="syncRevision"
              :marketplace="selectedMarketplace"
              @status="workflow[1] = $event"
              @selection="coreSelectionId = $event"
              @saved="businessSaved('core_terms')"
            />
          </div>
          <div v-show="selectedStep === 2" class="workspace-pane" data-step-pane="2">
            <p v-if="!savedInputRun || !rootsConfirmed" class="workspace-empty">
              先在第一步完成商品词相关度分析，随后在这里继续操作。
            </p>
            <CompetitorsPanel
              v-if="savedInputRun && rootsConfirmed && mountStep(2)"
              :key="`competitors-${savedInputRun.id}`"
              :run="savedInputRun"
              :sync-revision="syncRevision"
              :core-ready="workflow[1] === 'done'"
              :core-selection-id="coreSelectionId"
              :marketplace="selectedMarketplace"
              @status="workflow[2] = $event"
              @selection="competitorSelectionId = $event"
              @saved="businessSaved('competitor_selection')"
            />
          </div>
          <div v-show="selectedStep === 3" class="workspace-pane" data-step-pane="3">
            <p v-if="!savedInputRun || !rootsConfirmed" class="workspace-empty">
              先在第一步完成商品词相关度分析，随后在这里继续操作。
            </p>
            <AbaExpansionPanel
              v-if="savedInputRun && rootsConfirmed && mountStep(3)"
              :key="`expansion-${savedInputRun.id}`"
              v-model:report-week="reportWeek"
              :run="savedInputRun"
              :sync-revision="syncRevision"
              :core-ready="workflow[1] === 'done'"
              :competitors-ready="workflow[2] === 'done'"
              :core-selection-id="coreSelectionId"
              :competitor-selection-id="competitorSelectionId"
              :marketplace="selectedMarketplace"
              @status="
                if ($event === 'done' && workflow[3] !== 'done') sourceRevision++;
                workflow[3] = $event;
              "
            />
          </div>
          <div v-show="selectedStep === 4" class="workspace-pane" data-step-pane="4">
            <p v-if="!savedInputRun || !rootsConfirmed" class="workspace-empty">
              先在第一步完成商品词相关度分析，随后在这里继续操作。
            </p>
            <ThirdPartyPanel
              v-if="savedInputRun && rootsConfirmed && mountStep(4)"
              :key="`third-party-${savedInputRun.id}`"
              :run="savedInputRun"
              :sync-revision="syncRevision"
              :marketplace="selectedMarketplace"
              :core-ready="workflow[1] === 'done'"
              :competitors-ready="workflow[2] === 'done'"
              :core-selection-id="coreSelectionId"
              :competitor-selection-id="competitorSelectionId"
              @status="
                if ($event === 'done' && workflow[4] !== 'done') sourceRevision++;
                workflow[4] = $event;
              "
              @saved="businessSaved('third_party')"
            />
          </div>
          <div v-show="selectedStep === 5" class="workspace-pane" data-step-pane="5">
            <p v-if="!savedInputRun || !rootsConfirmed" class="workspace-empty">
              先在第一步完成商品词相关度分析，随后在这里继续操作。
            </p>
            <CleaningPanel
              v-if="savedInputRun && rootsConfirmed && mountStep(5)"
              :key="`cleaning-${savedInputRun.id}`"
              v-model:report-week="reportWeek"
              :run="savedInputRun"
              :ready="
                workflow[1] === 'done' &&
                workflow[2] === 'done' &&
                workflow[3] === 'done' &&
                workflow[4] === 'done'
              "
              :source-revision="syncRevision + sourceRevision"
              :core-selection-id="coreSelectionId"
              :competitor-selection-id="competitorSelectionId"
              @status="workflow[5] = $event"
              @saved="businessSaved('cleaning_review')"
            />
          </div>
          <div v-show="selectedStep === 6" class="workspace-pane" data-step-pane="6">
            <p v-if="!savedInputRun || !rootsConfirmed" class="workspace-empty">
              先在第一步完成商品词相关度分析，随后在这里继续操作。
            </p>
            <TrafficPanel
              v-if="savedInputRun && rootsConfirmed && mountStep(6)"
              :key="`traffic-${savedInputRun.id}`"
              :run="savedInputRun"
              :ready="workflow[5] === 'done'"
              :source-revision="syncRevision + sourceRevision"
              @status="workflow[6] = $event"
              @saved="businessSaved('traffic_review')"
            />
          </div>
          <div v-show="selectedStep === 7" class="workspace-pane" data-step-pane="7">
            <p v-if="!savedInputRun || !rootsConfirmed" class="workspace-empty">
              先在第一步完成商品词相关度分析，随后在这里继续操作。
            </p>
            <KeywordLibraryResults
              v-if="savedInputRun && rootsConfirmed && mountStep(7)"
              :key="`library-${savedInputRun.id}`"
              :run="savedInputRun"
              :ready="workflow[6] === 'done'"
              :source-revision="syncRevision + sourceRevision"
              @status="workflow[7] = $event"
              @saved="businessSaved('library_publish')"
            />
          </div>
        </WorkflowWorkspace>
        <footer class="page-footer">
          <span>关键词库工作流</span><span>进度以实际采集、审核与发布结果为准</span
          ><a href="#main">回到顶部 <ArrowRight :size="13" /></a>
        </footer>
      </div>
    </main>

    <Transition name="toast"
      ><div v-if="notification" class="toast-message">
        <CheckCircle2 :size="17" />{{ notification }}
      </div></Transition
    >
  </div>
</template>

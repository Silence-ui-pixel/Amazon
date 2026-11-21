import type { WorkflowView } from './api/workflow';

export type StepStatus = 'waiting' | 'running' | 'review' | 'done';
export const steps = [
  {
    title: '确认商品与商品词',
    note: '确认商品资料 → 确认四维基准 → 自动分析商品词',
    flow: ['确认商品资料', '确认四维基准', '分析相关度'],
  },
  {
    title: '确定核心大词',
    note: '逐商品词采集 ABA 25 万内周数据，四维 ≥40、词级相关度 ≥50% 后确认核心词',
    flow: ['查看商品词采集进度', '审核相关度与意图证据', '下载表格并保存核心词'],
  },
  {
    title: '筛选竞对 ASIN',
    note: '发现候选，核对款式与价格后保存',
    flow: ['发现候选', '核对商品', '保存竞对'],
  },
  {
    title: 'ABA 正反向拓词',
    note: '按所选报告周，采集正向与反向数据',
    flow: ['提交采集', '等待结果', '确认来源覆盖'],
  },
  {
    title: '卖家精灵拓词',
    note: '最近 30 天词表，确认映射和完整性',
    flow: ['获取词表', '确认映射', '核对完整性'],
  },
  {
    title: '词频分析与清洗',
    note: '四路关键词汇总后，清洗单词根并自动分类保存',
    flow: ['汇总四路关键词', '清洗单词根', '自动分类并保存'],
  },
  {
    title: '筛选流量入口',
    note: '核对相关性，保存保留决定与优先级',
    flow: ['生成筛选快照', '审核关键词', '保存决定'],
  },
  {
    title: '层级填充与发布',
    note: '复核第七步入库结果后发布版本',
    flow: ['查看入库结果', '确认发布'],
  },
];
// 后端细节点映射到前端八步；新增/更名节点时应同步维护，不能按数组序号猜步骤。
const nodeGroups = [
  ['ensure_input', 'identify_product', 'product_confirmation', 'generate_roots', 'root_selection'],
  ['week', 'collect_core_report', 'core_terms'],
  ['competitor_discovery', 'competitor_selection'],
  ['submit_sources', 'wait_sources'],
  ['third_party'],
  ['create_cleaning', 'cleaning_review'],
  ['create_traffic', 'traffic_review'],
  ['create_library', 'library_publish'],
];
export function stepForNode(key: string | null | undefined): number {
  return nodeGroups.findIndex((group) => !!key && group.includes(key));
}
export function currentStep(statuses: StepStatus[], view: WorkflowView | null): number {
  if (view && !['completed', 'aborted', 'superseded', 'incompatible'].includes(view.status)) {
    const index = stepForNode(
      view.current_node ?? view.nodes.find((n) => n.status === 'failed')?.key,
    );
    if (index >= 0) return index;
  }
  const index = statuses.findIndex((status) => status !== 'done');
  return index < 0 ? 7 : index;
}
export function prerequisites(index: number): number[] {
  return [[], [0], [1], [1, 2], [1, 2], [3, 4], [5], [6]][index] ?? [];
}

import type { StepStatus } from './workflowGuide';

type TaskStep = {
  title: string;
  flow: string[];
};

type TaskEvent = {
  node_label: string;
  status: string;
  message: string;
  output_summary: Record<string, string>;
};

type TaskWorkflow = {
  status: string;
  current_node: string | null;
  events: TaskEvent[];
};

export type TaskCenterInput = {
  current: number;
  selected: number;
  facts: StepStatus[];
  steps: TaskStep[];
  instruction: string;
  error?: string;
  workflow: TaskWorkflow | null;
};

export type TaskCenterMode = 'human' | 'automatic' | 'error' | 'complete';

export type TaskCenterView = {
  mode: TaskCenterMode;
  kicker: string;
  title: string;
  description: string;
  primaryLabel: string;
  primaryStep: number;
  actions: string[];
  outputTitle: string;
  outputMessage: string;
  outputs: [string, string][];
  outputEmpty: string;
  nextTitle: string;
  nextDescription: string;
};

const automaticStatuses = new Set(['queued', 'running', 'waiting_external']);

function resolveMode(input: TaskCenterInput): TaskCenterMode {
  if (input.error || input.workflow?.status === 'failed') return 'error';
  if (input.workflow?.status === 'completed') return 'complete';
  if (input.workflow && automaticStatuses.has(input.workflow.status)) return 'automatic';
  if (!input.workflow && input.facts.every((status) => status === 'done')) return 'complete';
  return 'human';
}

export function buildTaskCenter(input: TaskCenterInput): TaskCenterView {
  const mode = resolveMode(input);
  const currentStep = input.steps[input.current];
  const latestOutput = [...(input.workflow?.events ?? [])]
    .reverse()
    .find((event) => event.status === 'done' && Object.keys(event.output_summary).length > 0);
  const outputs = Object.entries(latestOutput?.output_summary ?? {}).map(
    ([label, value]) => [label, String(value)] as [string, string],
  );
  const complete = mode === 'complete';
  const hasSavedEvents = (input.workflow?.events.length ?? 0) > 0;
  const stepNumber = Math.min(input.current + 1, input.steps.length);

  const copy = {
    human: {
      kicker: `需要人工介入 · 第 ${stepNumber} 步`,
      primaryLabel: '去处理当前任务',
    },
    automatic: {
      kicker: `系统自动执行中 · 第 ${stepNumber} 步`,
      primaryLabel: '查看当前执行步骤',
    },
    error: {
      kicker: `需要处理 · 第 ${stepNumber} 步`,
      primaryLabel: '去处理失败步骤',
    },
    complete: {
      kicker: '研究已完成',
      primaryLabel: '查看发布结果',
    },
  }[mode];

  return {
    mode,
    kicker: copy.kicker,
    title: complete ? '关键词库已发布' : (currentStep?.title ?? '当前任务'),
    description: input.error || input.instruction,
    primaryLabel: copy.primaryLabel,
    primaryStep: complete ? input.steps.length - 1 : input.current,
    actions:
      mode === 'human'
        ? [...(currentStep?.flow ?? []), '保存后系统自动继续']
        : [...(currentStep?.flow ?? [])],
    outputTitle: latestOutput?.node_label ?? '系统刚刚完成',
    outputMessage: latestOutput?.message ?? '',
    outputs,
    outputEmpty:
      !hasSavedEvents && !['queued', 'running'].includes(input.workflow?.status ?? '')
        ? '旧记录或当前步骤未保存输出摘要，可展开运行详情查看节点状态。'
        : '当前步骤尚未产生输出摘要。',
    nextTitle: complete
      ? '查看和导出关键词库'
      : (input.steps[input.current + 1]?.title ?? '查看和导出关键词库'),
    nextDescription: complete
      ? '前往发布结果查看分层关键词并导出。'
      : mode === 'human'
        ? '完成当前人工操作并保存后，系统会自动进入下一步。'
        : '当前任务完成后，系统会自动衔接；需要人工确认时会暂停提示。',
  };
}

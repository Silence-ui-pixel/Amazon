const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');

const mod = { exports: {} };
const source = fs.readFileSync('src/workflowTaskCenter.ts', 'utf8');
const code = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText;
new Function('require', 'module', 'exports', code)(require, mod, mod.exports);
const { buildTaskCenter } = mod.exports;

const steps = Array.from({ length: 8 }, (_, index) => ({
  title: `步骤 ${index + 1}`,
  flow: [`操作 ${index + 1}A`, `操作 ${index + 1}B`],
}));
const facts = ['done', 'review', 'waiting', 'waiting', 'waiting', 'waiting', 'waiting', 'waiting'];

const human = buildTaskCenter({
  current: 1,
  selected: 1,
  facts,
  steps,
  instruction: '审核 10 个核心词并保存。',
  error: '',
  workflow: {
    status: 'waiting_human',
    current_node: 'core_terms',
    events: [
      {
        node_label: '核心大词分析',
        status: 'done',
        message: '已生成候选',
        output_summary: { 扫描范围: '1000', 候选结果: '10' },
      },
    ],
  },
});
assert.equal(human.mode, 'human');
assert.equal(human.kicker, '需要人工介入 · 第 2 步');
assert.equal(human.primaryLabel, '去处理当前任务');
assert.equal(human.primaryStep, 1);
assert.deepEqual(human.actions, ['操作 2A', '操作 2B', '保存后系统自动继续']);
assert.equal(human.outputTitle, '核心大词分析');
assert.deepEqual(human.outputs, [
  ['扫描范围', '1000'],
  ['候选结果', '10'],
]);
assert.equal(human.nextTitle, '步骤 3');

const automatic = buildTaskCenter({
  current: 3,
  selected: 1,
  facts: ['done', 'done', 'done', 'running', 'waiting', 'waiting', 'waiting', 'waiting'],
  steps,
  instruction: '系统正在采集。',
  error: '',
  workflow: { status: 'running', current_node: 'submit_sources', events: [] },
});
assert.equal(automatic.mode, 'automatic');
assert.equal(automatic.primaryLabel, '查看当前执行步骤');
assert.match(automatic.kicker, /系统自动执行中/);
assert.equal(automatic.outputEmpty, '当前步骤尚未产生输出摘要。');

const failed = buildTaskCenter({
  current: 5,
  selected: 2,
  facts: Array(8).fill('waiting'),
  steps,
  instruction: '普通说明',
  error: 'MODEL_OUTPUT_INVALID',
  workflow: { status: 'failed', current_node: 'cleaning_review', events: [] },
});
assert.equal(failed.mode, 'error');
assert.equal(failed.primaryLabel, '去处理失败步骤');
assert.equal(failed.description, 'MODEL_OUTPUT_INVALID');

const failedWithOldFacts = buildTaskCenter({
  current: 5,
  selected: 5,
  facts: Array(8).fill('done'),
  steps,
  instruction: '普通说明',
  error: 'RETRY_FAILED',
  workflow: { status: 'failed', current_node: 'cleaning_review', events: [] },
});
assert.equal(failedWithOldFacts.mode, 'error', '旧的完成事实不能遮住当前失败工作流');

const runningWithOldFacts = buildTaskCenter({
  current: 2,
  selected: 2,
  facts: Array(8).fill('done'),
  steps,
  instruction: '重新分析中',
  error: '',
  workflow: { status: 'running', current_node: 'competitor_discovery', events: [] },
});
assert.equal(runningWithOldFacts.mode, 'automatic', '旧的完成事实不能遮住重新运行状态');

const legacy = buildTaskCenter({
  current: 4,
  selected: 4,
  facts: Array(8).fill('waiting'),
  steps,
  instruction: '等待采集。',
  error: '',
  workflow: { status: 'waiting_external', current_node: 'third_party', events: [] },
});
assert.equal(legacy.outputEmpty, '旧记录或当前步骤未保存输出摘要，可展开运行详情查看节点状态。');

const completed = buildTaskCenter({
  current: 7,
  selected: 7,
  facts: Array(8).fill('done'),
  steps,
  instruction: '已发布。',
  error: '',
  workflow: { status: 'completed', current_node: null, events: [] },
});
assert.equal(completed.mode, 'complete');
assert.equal(completed.nextTitle, '查看和导出关键词库');
assert.equal(completed.primaryLabel, '查看发布结果');
assert.equal(completed.primaryStep, 7);
assert.equal(
  completed.outputEmpty,
  '旧记录或当前步骤未保存输出摘要，可展开运行详情查看节点状态。',
);

const completedBeforeFactsLoad = buildTaskCenter({
  current: 0,
  selected: 0,
  facts: Array(8).fill('waiting'),
  steps,
  instruction: '正在读取面板事实。',
  error: '',
  workflow: { status: 'completed', current_node: null, events: [] },
});
assert.equal(completedBeforeFactsLoad.mode, 'complete');
assert.equal(completedBeforeFactsLoad.primaryStep, 7, '服务端已完成时查看结果必须固定进入第八步');

console.log('workflow task center: human, automatic, error, legacy and completion states passed');

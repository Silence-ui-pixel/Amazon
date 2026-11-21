const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { parse, compileTemplate } = require('@vue/compiler-sfc');

function loadApi(response) {
  const source = fs.readFileSync('src/api/workflow.ts', 'utf8');
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  const mod = { exports: {} };
  new Function('require', 'module', 'exports', code)(
    () => ({
      ApiError: class ApiError extends Error {},
      request: async () => response,
    }),
    mod,
    mod.exports,
  );
  return mod.exports.workflowApi;
}

const baseView = {
  workflow_id: 'w1',
  run_id: 'r1',
  project_id: 'p1',
  status: 'running',
  revision: 2,
  current_node: 'core_terms',
  nodes: [{ key: 'core_terms', label: '核心大词', status: 'running', detail: '' }],
  waiting_for: null,
  error: '',
  next_poll_ms: 1500,
  created_at: '2026-11-01T12:00:00Z',
  updated_at: '2026-11-01T12:00:02Z',
};

const event = {
  sequence: 1,
  node_key: 'core_terms',
  node_label: '核心大词',
  status: 'running',
  attempt: 2,
  message: '正在筛选核心大词',
  input_summary: { 报告周: '2026-10-31' },
  output_summary: {},
  error: '',
  started_at: '2026-11-01T12:00:01Z',
  finished_at: null,
  duration_ms: null,
};

(async () => {
  const run = { id: 'r1', project_id: 'p1' };
  const parsed = await loadApi({ ...baseView, events: [event] }).latest(run);
  assert.equal(parsed.events[0].attempt, 2, '完整事件必须通过 API 运行时校验');

  const old = await loadApi(baseView).latest(run);
  assert.deepEqual(old.events, [], '旧工作流缺少 events 时必须兼容为空日志');

  await assert.rejects(
    () => loadApi({ ...baseView, events: [{ ...event, attempt: 'two' }] }).latest(run),
    /编排状态格式不正确/,
    '坏事件格式不得静默进入组件',
  );

  const { descriptor } = parse(fs.readFileSync('src/components/WorkflowBar.vue', 'utf8'));
  const compiled = compileTemplate({
    source: descriptor.template.content,
    filename: 'WorkflowBar.vue',
    id: 'workflow-log-test',
  });
  assert.equal(compiled.errors.length, 0, String(compiled.errors));
  const render = compiled.code;
  for (const visibleText of ['总体进度', '执行日志', '输入摘要', '输出结果', '错误信息']) {
    assert.match(render, new RegExp(visibleText), `面板必须显示“${visibleText}”`);
  }
  assert.match(render, /event\.attempt/, '日志必须展示重试 attempt');
  assert.match(render, /event\.duration_ms/, '日志必须展示每步耗时');
  assert.match(render, /view\.events/, '日志必须由持久化 events 渲染');
  for (const visibleText of ['旧记录步骤回放', '步骤作用', '执行结果', '旧记录未保存']) {
    assert.match(render, new RegExp(visibleText), `旧记录必须直接显示“${visibleText}”`);
  }
  assert.match(render, /nodePurpose/, '旧记录必须解释每个节点具体做什么');
  assert.match(render, /node\.detail/, '旧记录必须显示当时保存的节点结果');
  assert.doesNotMatch(
    render,
    /旧记录暂无执行日志，查看内部节点/,
    '旧记录步骤不能继续藏在折叠胶囊里',
  );
  console.log('workflow execution log: API contract and compiled panel passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { parse, compileScript } = require('@vue/compiler-sfc');

async function checkPreview(historyPreview, expectedResumes) {
  const { descriptor } = parse(fs.readFileSync('src/components/WorkflowBar.vue', 'utf8'));
  const script = compileScript(descriptor, { id: 'history-test' }).content;
  const code = ts.transpileModule(script, {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  const mounted = [];
  let resumed = 0;
  const view = {
    status: 'waiting_external',
    workflow_id: 'w',
    revision: 1,
    nodes: [],
    waiting_for: { ready_to_resume: true, gate: 'sources' },
    next_poll_ms: 3000,
  };
  const vue = {
    ...require('vue'),
    defineComponent: (x) => x,
    ref: (value) => ({ value }),
    computed: (getter) => ({
      get value() {
        return getter();
      },
    }),
    useModel: () => ({ value: '2026-10-24' }),
    onMounted: (callback) => mounted.push(callback),
    onBeforeUnmount: () => {},
    watch: () => {},
  };
  const api = {
    latest: async () => view,
    resume: async () => {
      resumed++;
      return view;
    },
  };
  const mod = { exports: {} };
  new Function('require', 'module', 'exports', code)(
    (name) => (name === 'vue' ? vue : { workflowApi: api }),
    mod,
    mod.exports,
  );
  global.window = { setTimeout: () => 1, clearTimeout: () => {} };
  mod.exports.default.setup(
    { run: { id: 'r', project_id: 'p' }, historyPreview },
    { expose: () => {}, emit: () => {} },
  );
  mounted.forEach((callback) => callback());
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(resumed, expectedResumes, '历史回看不能在打开页面时自动推进外部等待任务');
}
(async () => {
  const historyModule = { exports: {} };
  const historyCode = ts.transpileModule(fs.readFileSync('src/api/researchRecords.ts', 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  const requested = [];
  new Function('require', 'module', 'exports', historyCode)(
    () => ({
      request: async (url) => {
        requested.push(url);
        return [{ week_end: '2026-09-26' }];
      },
    }),
    historyModule,
    historyModule.exports,
  );
  assert.equal(
    await historyModule.exports.readResearchWeek({ id: 'r', project_id: 'p' }),
    '2026-09-26',
    '旧研究须恢复原报告周',
  );
  assert.equal(requested[0], '/api/v1/projects/p/pipeline-runs/r/aba-reports?limit=1');
  await checkPreview(true, 0);
  await checkPreview(false, 0);
  console.log('research history: opening any page never causes an automatic resume write');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

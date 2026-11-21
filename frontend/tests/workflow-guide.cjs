const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { parse, compileTemplate } = require('@vue/compiler-sfc');
const mod = { exports: {} };
const code = ts.transpileModule(fs.readFileSync('src/workflowGuide.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
new Function('require', 'module', 'exports', code)(require, mod, mod.exports);
const { currentStep, prerequisites, stepForNode } = mod.exports;
assert.equal(currentStep(Array(8).fill('waiting'), null), 0);
assert.equal(currentStep(['done', 'done', 'done', 'done', 'waiting', 'waiting', 'waiting', 'waiting'], null), 4);
assert.equal(currentStep(Array(8).fill('done'), null), 7);
assert.equal(currentStep(Array(8).fill('waiting'), {status:'waiting_human',current_node:'cleaning_review',nodes:[]}), 5);
assert.equal(stepForNode('library_publish'), 7);
assert.equal(stepForNode('not_a_node'), -1);
assert.deepEqual(prerequisites(4), [1, 2], 'parallel sources have identical preparation prerequisites');
assert.deepEqual(prerequisites(3), [1, 2]);
assert.deepEqual(prerequisites(5), [3, 4], 'both sources join before cleaning');
assert.deepEqual(prerequisites(0), []);

function compileVue(filename) {
  const { descriptor } = parse(fs.readFileSync(filename, 'utf8'));
  const compiled = compileTemplate({
    source: descriptor.template.content,
    filename,
    id: `workflow-guide-${filename}`,
  });
  assert.equal(compiled.errors.length, 0, String(compiled.errors));
  return compiled.code;
}

const workspace = compileVue('src/components/WorkflowWorkspace.vue');
for (const visibleText of ['你现在要做', '系统刚刚完成', '下一步自动衔接']) {
  assert.match(workspace, new RegExp(visibleText), `任务中心必须显示“${visibleText}”`);
}
assert.match(workspace, /taskCenter\.primaryLabel/, '主操作按钮必须由任务状态决定');
assert.match(workspace, /go\(_ctx\.taskCenter\.primaryStep\)/, '主操作目标必须由同一任务状态决定');
assert.match(workspace, /taskCenter\.outputs/, '系统输出必须来自状态投影');
assert.match(workspace, /保存后系统自动继续/, '人工操作必须解释保存后的自动衔接');
assert.match(workspace, /请先处理错误后重试/, '失败状态必须给出明确人工处理提示');

const app = compileVue('src/App.vue');
assert.match(app, /运行详情与报告周期/, '运行日志和报告周期必须放入次级详情');
assert.match(app, /details/, '次级运行详情必须默认收起');
console.log('workflow guide: new task, parallel join, gate mapping, publication passed');

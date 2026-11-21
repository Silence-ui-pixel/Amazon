const assert = require('node:assert/strict');
const fs = require('node:fs');
const { parse, compileTemplate } = require('@vue/compiler-sfc');

const api = fs.readFileSync('src/api/browserAgent.ts', 'utf8');
assert.match(api, /\/api\/v1\/browser-agent\/status/);
assert.match(api, /refresh/);
assert.doesNotMatch(api, /token|authorization/i, '前端 API 不得接触 Agent Token');

const source = fs.readFileSync('src/components/BrowserAgentStatus.vue', 'utf8');
const { descriptor } = parse(source);
const compiled = compileTemplate({
  source: descriptor.template.content,
  filename: 'BrowserAgentStatus.vue',
  id: 'browser-agent-status-test',
});
assert.equal(compiled.errors.length, 0, String(compiled.errors));
for (const text of [
  '采集端在线',
  '采集端离线',
  '正在采集',
  '需要人工处理',
  '重新检测',
  '请在紫鸟电脑',
]) {
  assert.match(source, new RegExp(text), `状态卡片必须展示“${text}”`);
}
assert.match(source, /browserAgentApi\.refresh/);
assert.doesNotMatch(source, /token|authorization/i, 'Token 不得进入组件 DOM');

const bar = fs.readFileSync('src/components/WorkflowBar.vue', 'utf8');
assert.match(bar, /BrowserAgentStatus/);
assert.match(bar, /从当前步骤继续/);
assert.doesNotMatch(
  bar,
  /next\.waiting_for\?\.ready_to_resume[\s\S]{0,200}await resume\('poll'/,
  '页面轮询不得绕过后端的一次自动恢复额度',
);
console.log('browser agent status: API and component contract passed');

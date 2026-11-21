const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { parse, compileTemplate } = require('@vue/compiler-sfc');

function loadApi(response) {
  const code = ts.transpileModule(fs.readFileSync('src/api/competitors.ts', 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  const mod = { exports: {} };
  new Function('require', 'module', 'exports', code)(
    () => ({ ApiError: class ApiError extends Error {}, request: async () => response }),
    mod,
    mod.exports,
  );
  return mod.exports.competitorApi;
}

const response = {
  id: 'c',
  core_selection_id: 'core',
  core_keywords: ['dumbbell rack'],
  version: 1,
  stale: false,
  confirmed_at: null,
  reference_price: '100',
  currency: 'USD',
  tolerance: '0.15',
  items: [],
  warnings: [],
  poe_downloads: [
    {
      id: 'f',
      filename: '筛选竞对ASIN-dumbbell rack.csv',
      keyword: 'dumbbell rack',
      sha256: 'abc',
      source_url: 'https://sellercentral.amazon.com/opportunity-explorer/explore/niche/x/product',
      rows: 567,
      intent_ref: '/opportunity-explorer/explore/niche/x',
      intent_name: 'adjustable dumbbell stand',
      intent_reason: '用途和结构最接近',
      intent_confidence: 'medium',
      status: 'completed',
      stage: '',
      error: '',
      price_qualified_rows: 43,
      relevance_qualified_rows: 12,
    },
  ],
};

(async () => {
  const parsed = await loadApi(response).get({ id: 'r', project_id: 'p' });
  assert.equal(parsed.poe_downloads[0].intent_name, 'adjustable dumbbell stand');
  assert.equal(parsed.poe_downloads[0].price_qualified_rows, 43);

  // 线上完成状态是 completed；旧版本保留的空阶段也必须可读。
  for (const stage of ['', 'completed']) {
    const data = structuredClone(response);
    data.poe_downloads[0].stage = stage;
    const result = await loadApi(data).get({ id: 'r', project_id: 'p' });
    assert.equal(result.poe_downloads[0].stage, stage);
  }

  // 后端未提供逐报表相关度数量时，保留未知，不能伪造为零或拒绝整份结果。
  const withoutCount = structuredClone(response);
  delete withoutCount.poe_downloads[0].relevance_qualified_rows;
  const restored = await loadApi(withoutCount).get({ id: 'r', project_id: 'p' });
  assert.equal(restored.poe_downloads[0].relevance_qualified_rows, undefined);
  for (const count of [0, 12]) {
    const data = structuredClone(response);
    data.poe_downloads[0].relevance_qualified_rows = count;
    const result = await loadApi(data).get({ id: 'r', project_id: 'p' });
    assert.equal(result.poe_downloads[0].relevance_qualified_rows, count);
  }
  for (const count of ['12', null, -1, 1.5]) {
    const data = structuredClone(response);
    data.poe_downloads[0].relevance_qualified_rows = count;
    await assert.rejects(() => loadApi(data).get({ id: 'r', project_id: 'p' }), /竞对接口返回结构不正确/);
  }
  // 失败证据必须保留并可读，未知阶段仍视为契约错误。
  for (const stage of ['search', 'archive', 'selection', 'download', 'parse', 'intent_collection', 'intent_selection', 'csv_validation']) {
    const data = structuredClone(response);
    Object.assign(data.poe_downloads[0], { status: 'failed', stage, error: '采集阶段失败' });
    const result = await loadApi(data).get({ id: 'r', project_id: 'p' });
    assert.equal(result.poe_downloads[0].stage, stage);
    assert.equal(result.poe_downloads[0].error, '采集阶段失败');
  }
  for (const stage of ['unknown-stage', null, 12]) {
    const data = structuredClone(response);
    data.poe_downloads[0].stage = stage;
    await assert.rejects(() => loadApi(data).get({ id: 'r', project_id: 'p' }), /竞对接口返回结构不正确/);
  }
  if (process.env.COMPETITOR_RESPONSE_FIXTURE) {
    const actual = JSON.parse(fs.readFileSync(process.env.COMPETITOR_RESPONSE_FIXTURE, 'utf8'));
    const result = await loadApi(actual).get({ id: actual.run_id, project_id: actual.project_id });
    assert.equal(result.core_keywords.length, 4);
    assert.equal(result.items.length, 12);
    assert.equal(result.poe_downloads.length, 4);
    console.log('Production response replay: 4 core keywords, 12 competitors, 4 downloads restored');
  }

  await assert.rejects(
    () => loadApi({ ...response, poe_downloads: [{ ...response.poe_downloads[0], status: 'done' }] }).get({ id: 'r', project_id: 'p' }),
    /竞对接口返回结构不正确/,
  );

  const { descriptor } = parse(fs.readFileSync('src/components/CompetitorsPanel.vue', 'utf8'));
  const compiled = compileTemplate({
    source: descriptor.template.content,
    filename: 'CompetitorsPanel.vue',
    id: 'competitor-intent-test',
  });
  assert.equal(compiled.errors.length, 0, String(compiled.errors));
  for (const text of ['所选购买意图', '选择理由', '30 天商品', '价格通过', '相关度通过', '最近 30 天平均售价']) {
    assert.match(compiled.code, new RegExp(text), `方法三必须显示“${text}”`);
  }
  assert.match(compiled.code, /intent_confidence/);
  assert.match(compiled.code, /file\.status/);
  assert.match(compiled.code, /file\.stage/);
  assert.match(compiled.code, /retryPoe/);
  console.log('competitor intent workflow: API contract and status panel passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

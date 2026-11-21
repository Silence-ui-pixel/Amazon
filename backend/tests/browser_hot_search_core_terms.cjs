// Real Vite UI; only HTTP boundaries are replaced, with no external services.
// Catches: page-limited saves, missing evidence, permissive eligibility, unstable selection order,
// stale writes, status loss, legacy regressions and page-level horizontal overflow.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const path = require('node:path');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const project = '11111111-1111-4111-8111-111111111111';
const runId = '22222222-2222-4222-8222-222222222222';
const id = n => `33333333-3333-4333-8333-${String(n).padStart(12, '0')}`;
const stamp = '2026-10-29T00:00:00Z';
const profile = { asin: 'B000000001', purpose: ['支撑大门'], style_structure: ['轮子'], specifications: ['户外'], price: '20', currency: 'USD', package_quantity: '1', unit_basis: 'each', price_source: 'asin_current' };
const product = { product_name: '大门轮', product_type: 'gate wheel', features: ['支撑大门'], uncertainties: [] };
const report = { id: id(100), project_id: project, run_id: runId, source: 'sellercentral_internal', marketplace: 'US', week_start: '2026-10-18', week_end: '2026-10-24', fetched_at: stamp, max_rows: 1000, query_mode: 'product_term', rule_version: 'hot-search-core-v1', roots_snapshot: ['gate wheel'], query_terms_snapshot: ['gate wheel'], stale: false, warnings: [], reports: [] };
const legacyReport = { ...report, id: id(101), query_mode: 'root', rule_version: 'legacy-root-v1', query_terms_snapshot: [], reports: [{ query: 'gate wheel', query_mode: 'root', total: 1, fetched: 1, pages: 1, truncated: false, stop_reason: 'complete', rows: [{ keyword: 'legacy gate wheel', search_frequency_rank: 10, products: [] }] }] };
const evidence = { object_match: true, purpose_match: true, product_level_match: true, modifiers_compatible: true, category: 'exact', reasons: { object_match: '对象为大门轮', purpose_match: '用途支撑大门', product_level_match: '整件商品', modifiers_compatible: '户外修饰兼容' }, explicit_conflict: false, accessory_target: false, broad_category: false };
const comparison = { asin: 'B000000002', status: 'high_related', purpose_score: 90, style_structure_score: 88, specifications_score: 84, price_score: '95', overall_score: '88.55', high_related: true, purpose_reason: '用途一致', style_structure_reason: '结构一致', specifications_reason: '规格接近', price_reason: '价差5%', exclusion_reason: '', natural_position: 1, candidate_profile: profile };
const makeItem = n => ({ candidate_id: id(n), keyword: `gate wheel ${String(n).padStart(2, '0')}`, search_frequency_rank: n === 40 ? 250000 : n, source_product_terms: ['gate wheel'], brand_category: 'generic', intent_evidence: structuredClone(evidence), summary: { valid_samples: 5, high_related_count: 4, relevance_percent: '80', confidence: 'low' }, comparisons: [comparison, { ...comparison, asin: 'B000000003', status: 'excluded', high_related: false, overall_score: null, exclusion_reason: '缺少价格资料' }], status: 'completed', exclusion_reasons: [] });
const items = Array.from({ length: 40 }, (_, i) => makeItem(i + 1));
for (const [n, changes] of [
  [41, { search_frequency_rank: 250001 }],
  [42, { summary: { valid_samples: 5, high_related_count: 3, relevance_percent: '60', confidence: 'low' } }],
  [43, { intent_evidence: { ...evidence, object_match: false, category: 'broad', broad_category: true }, exclusion_reasons: ['intent_not_exact'] }],
  [44, { intent_evidence: { ...evidence, product_level_match: false, category: 'accessory', accessory_target: true }, exclusion_reasons: ['intent_not_exact'] }],
  [45, { brand_category: 'own_brand', exclusion_reasons: ['brand_not_generic'] }],
  [46, { status: 'no_samples', summary: { valid_samples: 0, high_related_count: 0, relevance_percent: null, confidence: 'none' }, comparisons: [], exclusion_reasons: ['no_valid_samples'] }],
  [47, { status: 'failed', summary: null, comparisons: [], exclusion_reasons: ['collection_failed'] }],
  [48, { brand_category: 'competitor_brand', exclusion_reasons: ['brand_not_generic'] }],
  [49, { brand_category: 'brand_unknown', exclusion_reasons: ['brand_not_generic'] }],
]) items.push({ ...makeItem(n), ...changes });
const analysis = { id: id(200), project_id: project, run_id: runId, report_id: report.id, fingerprint: 'fixture', rule_version: 'hot-search-core-v1', profile_version: 2, profile_snapshot: profile, own_brands_snapshot: [], marketplace: 'US', delivery_region: '10001', input_snapshot: {}, status: 'completed', complete: true, stale: false, source_partial: false, items, recommended_candidate_ids: items.slice(0, 40).map(x => x.candidate_id), batches: [], collections: Object.fromEntries(items.map(x => [x.candidate_id, { term: x.keyword, raw_natural_count: 6, unique_asins: ['B000000002', 'B000000003'], products: [], failures: [], pages: [] }])), snapshots: {}, snapshot_failures: {}, created_at: stamp };
report.reports = [{ query: 'gate wheel', query_mode: 'product_term', total: items.length, fetched: items.length, pages: 1, truncated: false, stop_reason: 'complete', rows: items.map(x => ({ keyword: x.keyword, search_frequency_rank: x.search_frequency_rank, products: [] })) }];

// Reuse an existing Vite instance, otherwise own its process and stop it on every exit path.
async function viteServer() {
  const ready = async () => {
    try { const response = await fetch('http://127.0.0.1:5173/', { signal: AbortSignal.timeout(1000) }); return response.ok && (await response.text()).includes('/@vite/client'); }
    catch { return false; }
  };
  if (await ready()) return async () => {};
  const cwd = path.resolve(__dirname, '../../frontend');
  const server = spawn(process.execPath, [path.join(cwd, 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', '5173', '--strictPort'], { cwd, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  let output = '', failure;
  server.stdout.on('data', chunk => { output += chunk; });
  server.stderr.on('data', chunk => { output += chunk; });
  server.on('error', error => { failure = error; });
  const stop = async () => {
    if (server.exitCode !== null || failure) return;
    server.kill();
    await Promise.race([new Promise(resolve => server.once('exit', resolve)), new Promise(resolve => setTimeout(resolve, 2000))]);
  };
  for (let i = 0; i < 60; i++) {
    if (await ready()) return stop;
    if (failure || server.exitCode !== null) break;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  await stop();
  throw new Error(`Vite did not start: ${failure || output}`);
}

(async () => {
  const stopServer = await viteServer();
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let current = structuredClone(analysis), saved = null, payload, missingLatest = false, missingReports = false, collectPayload;
    const requests = [];
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url()).pathname;
      if (!url.startsWith('/api/')) return route.continue();
      const method = route.request().method();
      const currentReport = current.rule_version === 'hot-search-core-v1' ? report : legacyReport;
      requests.push(`${method} ${url}`);
      let data;
      if (url.endsWith(`/pipeline-runs/${runId}`)) data = { id: runId, project_id: project, status: 'input_received', input: { marketplace: 'US', asin: 'B000000001', product_name: null, description: '', image_ids: [], image_urls: [], reference_price: null } };
      else if (url.endsWith('/product-materials')) data = { schema_version: 1, project_id: project, run_id: runId, marketplace: 'US', asin: 'B000000001', supplied_product_name: null, supplied_description: '', reference_price: null, reference_currency: 'USD', images: [], asin_resolution: 'resolved', asin_product: null, model_status: 'succeeded', warnings: [] };
      else if (url.endsWith('/product-understanding')) data = { project_id: project, run_id: runId, provider: 'bailian', model: 'qwen3.8-max', created_at: stamp, product, warnings: [] };
      else if (url.endsWith('/product-confirmation/history')) data = [{ project_id: project, run_id: runId, version: 1, created_at: stamp, data: { expected_version: 0, product, reviewer: '运营', reason: '确认' } }];
      else if (url.endsWith('/product-profile')) data = { project_id: project, run_id: runId, version: 2, status: 'confirmed', profile, created_at: stamp };
      else if (url.endsWith('/root-candidates')) data = { project_id: project, run_id: runId, model: 'qwen3.8-max', marketplace: 'US', language: 'en', created_at: stamp, rule_version: current.rule_version === 'hot-search-core-v1' ? 'product-term-v1' : 'legacy-root-v1', candidates: [{ id: id(500), text: 'gate wheel', translation: '大门轮', reason: '同类商品', expansions: [] }], warnings: [] };
      else if (url.endsWith('/root-selection')) data = { project_id: project, run_id: runId, candidate_ids: [id(500)], confirmed_at: stamp, source: 'automatic', verification_id: id(501) };
      else if (url.endsWith('/aba-reports') && method === 'POST') { collectPayload = route.request().postDataJSON(); missingReports = false; data = currentReport; }
      else if (url.endsWith('/aba-reports')) data = missingReports ? [] : [currentReport];
      else if (url.endsWith(`/aba-reports/${currentReport.id}`)) data = currentReport;
      else if (url.endsWith('/hot-search-candidates')) data = items.filter(x => x.search_frequency_rank <= 250000).map(x => ({ ...x, status: 'waiting', summary: null, intent_evidence: null, comparisons: [], exclusion_reasons: [] }));
      else if (url.endsWith('/core-candidates') && url.includes(`/aba-reports/${report.id}/`)) { await route.fulfill({ status: 409, json: { error: { code: 'CORE_FORWARD_REPORT_REQUIRED', message: '第二步需要词根正向周报' } } }); return; }
      else if (url.endsWith('/core-candidates')) data = { report_id: legacyReport.id, total: current.items.length, offset: 0, limit: 10, source_partial: false, items: current.items };
      else if (url.endsWith('/core-analyses')) data = current.rule_version === 'hot-search-core-v1' ? [] : [current];
      else if (url.endsWith('/hot-search-core-analyses') && method === 'POST') { data = current = { ...structuredClone(analysis), status: 'queued', complete: false, recommended_candidate_ids: [] }; }
      else if (url.endsWith(`/hot-search-core-analyses/${analysis.id}/execute`) && method === 'POST') { data = current = structuredClone(analysis); }
      else if (url.endsWith('/hot-search-core-analysis') && current.rule_version === 'hot-search-core-v1' && !missingLatest || url.endsWith(`/hot-search-core-analyses/${analysis.id}`) || url.endsWith(`/core-analyses/${id(201)}`)) data = current;
      else if (url.endsWith(`/hot-search-core-analyses/${id(202)}`)) data = { ...structuredClone(analysis), id: id(202), stale: true, status: 'stale' };
      else if (url.endsWith('/core-selection') && method === 'PUT') {
        payload = route.request().postDataJSON();
        saved = { id: id(300), project_id: project, run_id: runId, analysis_id: current.id, rule_version: payload.rule_version, source_analysis_id: current.id, report_id: currentReport.id, confirmed_at: stamp, version: 1, stale: false, items: current.items.filter(x => payload.candidate_ids.includes(x.candidate_id)).map(x => ({ ...x, id: x.candidate_id, matched_roots: [], root_reference: false, keyword_role: 'core', translation: '', relevance: 'high', intent: 'exact', category: 'generic', reason: '热门搜索词精准意图及四维相关度门槛通过', recommended: true })) };
        data = saved;
      } else if (url.endsWith('/core-selection') && saved) data = saved;
      else { await route.fulfill({ status: 404, json: { error: { message: 'offline fixture' } } }); return; }
      await route.fulfill({ status: 200, json: data });
    });
    const panel = page.locator('section.core-panel[aria-label="确定核心大词"]');
    async function open() {
      await page.goto(`http://127.0.0.1:5173/?project=${project}&run=${runId}`);
      try { await page.getByRole('button', { name: /确定核心大词/ }).first().click({ timeout: 8000 }); }
      catch (error) { console.error(await page.locator('body').innerText(), requests, errors); throw error; }
      await panel.waitFor();
      await panel.getByText('80.00%').first().waitFor({ timeout: 8000 });
    }
    await open();
    assert.equal(await panel.getByLabel('周报记录').inputValue(), report.id, 'restore actual product_term report without a confirmed selection');
    assert.equal(await panel.locator('tbody > tr').count(), 10, 'default display must paginate 10 rows');
    assert.equal(await panel.getByLabel('每页显示数量').inputValue(), '10');
    for (const text of ['来源商品词', '报告周', '自然取得', '有效样本', '高相关数', '相关度', '可信度', '对象一致', '用途一致', '商品层级', '修饰兼容']) assert.ok((await panel.innerText()).includes(text), text);
    assert.ok((await panel.innerText()).includes('2026-10-24'));
    assert.ok((await panel.innerText()).includes('低可信'));
    assert.equal(await panel.getByLabel('选择 gate wheel 01', { exact: true }).isChecked(), true);
    assert.equal(await panel.locator('.selected-summary, .selected-terms, [aria-label="已选核心词"]').count(), 0);
    await panel.locator('tbody > tr').first().locator('summary').click();
    await panel.getByText('综合 88.55').first().waitFor();
    await panel.getByText('缺少价格资料').first().waitFor();
    await panel.getByLabel('选择 gate wheel 01', { exact: true }).click();
    assert.ok((await panel.locator('tbody > tr').first().innerText()).includes('gate wheel 02'));
    // Re-select on the last page: chosen items return to the original table, in backend order.
    await panel.getByLabel('每页显示数量').selectOption('20');
    await panel.getByRole('button', { name: '下一页', exact: true }).click();
    await panel.getByLabel('选择 gate wheel 01', { exact: true }).click();
    await panel.getByRole('button', { name: '上一页', exact: true }).click();
    assert.ok((await panel.locator('tbody > tr').first().innerText()).includes('gate wheel 01'));
    await panel.getByRole('button', { name: '确认核心大词', exact: true }).click();
    await panel.getByText('已保存核心大词', { exact: true }).waitFor();
    assert.deepEqual(payload, { analysis_id: id(200), rule_version: 'hot-search-core-v1', candidate_ids: Array.from({ length: 40 }, (_, i) => id(i + 1)), expected_version: 0 });
    await page.reload();
    await page.locator('.step-nav-item[data-step="1"]').click();
    await panel.getByText('已保存核心大词', { exact: true }).waitFor();
    assert.equal(await panel.getByLabel('每页显示数量').inputValue(), '10', 'restoring all 40 choices retains default display pagination');
    assert.equal(await panel.locator('tbody > tr').count(), 10);
    await panel.getByLabel('每页显示数量').selectOption('20');
    await panel.getByRole('button', { name: '下一页', exact: true }).click();
    await panel.getByRole('button', { name: '下一页', exact: true }).click();
    for (let n = 41; n <= 49; n++) assert.equal(await panel.getByLabel(`选择 gate wheel ${n}`, { exact: true }).isDisabled(), true);
    for (const text of ['250,001', '60.00%', '相关度未超过 60%', 'ABA 排名超过 250,000', '宽泛意图', '配件意图', '自家品牌', '竞品品牌', '品牌待核对', '无有效样本', '采集失败']) assert.ok((await panel.innerText()).includes(text), text);
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `no page overflow at ${width}px`);
    }
    saved = null;
    current = { ...structuredClone(analysis), stale: true, status: 'stale' };
    await page.reload();
    await panel.getByText('历史结果已失效', { exact: false }).first().waitFor();
    assert.equal(await panel.getByRole('button', { name: '确认核心大词', exact: true }).isDisabled(), true);
    assert.equal(await panel.getByRole('button', { name: '分析全部搜索词', exact: true }).isDisabled(), false);
    current = { ...structuredClone(analysis), status: 'running', complete: false };
    current.items[0].status = 'running';
    await page.reload();
    await panel.getByText(/逐词进度/).first().waitFor();
    assert.equal(await panel.getByLabel('选择 gate wheel 01', { exact: true }).isDisabled(), true);
    current = { ...structuredClone(analysis), status: 'incomplete', complete: false, own_brands_snapshot: ['Acme'] };
    await page.reload();
    await panel.getByText('80.00%').first().waitFor();
    assert.equal(await panel.getByLabel('自家品牌（选填）').inputValue(), 'Acme', 'restore latest snapshot brands before reviewing');
    assert.equal(await panel.getByRole('button', { name: '确认核心大词', exact: true }).isDisabled(), false, 'backend allows eligible terms from terminal incomplete analyses');
    saved = null;
    payload = null;
    current = { ...structuredClone(analysis), status: 'completed', complete: true, items: analysis.items.slice(41, 45), recommended_candidate_ids: [] };
    await page.reload();
    await panel.getByText('自动筛选没有合格核心词', { exact: false }).waitFor();
    assert.equal(await panel.getByLabel('选择 gate wheel 42', { exact: true }).isDisabled(), false, 'manual fallback must enable candidates within ABA 250,000');
    await panel.getByLabel('选择 gate wheel 42', { exact: true }).click();
    await panel.getByRole('button', { name: '确认核心大词', exact: true }).click();
    await panel.getByText('已保存核心大词', { exact: true }).waitFor();
    assert.deepEqual(payload.candidate_ids, [id(42)]);
    current = { ...structuredClone(analysis), status: 'queued', complete: false, items: analysis.items.map(x => ({ ...x, status: 'waiting' })), recommended_candidate_ids: [] };
    await page.reload();
    await panel.getByText('分析已落盘；可继续执行已排队分析。', { exact: true }).waitFor();
    assert.equal(await panel.getByRole('button', { name: '确认核心大词', exact: true }).isDisabled(), true);
    current = { id: id(201), report_id: legacyReport.id, status: 'succeeded', model: 'qwen3.8-max', request: { candidate_ids: [], offset: 0, limit: 10, own_brands: [] }, stale: false, items: [{ id: id(1), keyword: 'legacy gate wheel', search_frequency_rank: 10, matched_roots: ['gate wheel'], root_reference: false, keyword_role: 'core', translation: '历史大门轮', relevance: 'high', intent: 'exact', category: 'generic', reason: '历史模型判断依据', recommended: true }], warnings: [], source_analysis_ids: [] };
    await page.reload();
    await panel.getByText('历史大门轮', { exact: true }).waitFor();
    assert.equal(await panel.getByRole('columnheader', { name: '搜索词 / 中文', exact: true }).count(), 1);
    assert.equal(await panel.getByRole('columnheader', { name: '四项精准意图证据', exact: true }).count(), 0);
    await panel.getByText('历史模型判断依据', { exact: true }).waitFor();
    assert.equal(requests.some(x => x.startsWith('POST ')), false, 'restore must be read-only');
    current = structuredClone(analysis);
    saved = { id: id(301), analysis_id: id(202), rule_version: 'hot-search-core-v1', report_id: report.id, version: 2, stale: true, items: [{ id: id(49), keyword: 'old selection' }] };
    await page.reload();
    await panel.getByText('80.00%').first().waitFor();
    assert.equal(await panel.getByLabel('选择 gate wheel 01', { exact: true }).isChecked(), true, 'current B recommendations replace stale A selection');
    assert.equal(await panel.getByRole('button', { name: '确认核心大词', exact: true }).isDisabled(), false);
    saved = null;
    current = structuredClone(analysis);
    missingLatest = true;
    missingReports = true;
    await page.reload();
    const collected = page.waitForResponse(response => response.url().endsWith('/aba-reports') && response.request().method() === 'POST');
    await panel.getByRole('button', { name: '获取 ABA 周报', exact: true }).click();
    await collected;
    assert.deepEqual(collectPayload, { query_mode: 'product_term', max_rows: 1000 });
    const submitted = page.waitForRequest(request => request.url().endsWith('/hot-search-core-analyses') && request.method() === 'POST');
    await panel.getByRole('button', { name: '分析全部搜索词', exact: true }).click();
    assert.deepEqual((await submitted).postDataJSON(), { report_id: id(100), own_brands: [] });
    await panel.getByText('80.00%').first().waitFor();
    assert.equal(requests.filter(x => x === `POST /api/v1/projects/${project}/pipeline-runs/${runId}/hot-search-core-analyses/${analysis.id}/execute`).length, 1);
    assert.equal(requests.some(x => x.includes(`/aba-reports/${report.id}/core-candidates`)), false, 'product_term must never call the legacy 409 endpoint');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ all40Saved: true, paginated: true, evidenceVisible: true, stableSelectedRows: true, staleBlocked: true, progressVisible: true, incompleteAllowed: true, legacyColumns: true, noHorizontalOverflow: true, externalCalls: 0 }));
  } finally { if (browser) await browser.close(); await stopServer(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

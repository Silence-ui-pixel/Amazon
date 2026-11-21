// 离线浏览器回归：验证商品词最终结果、摘要折叠、可信度和刷新恢复。
const assert = require('node:assert/strict');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const project = '11111111-1111-4111-8111-111111111111';
const runId = '22222222-2222-4222-8222-222222222222';
const candidate = '33333333-3333-4333-8333-333333333333';
const analysisId = '44444444-4444-4444-8444-444444444444';
const product = { product_name: '大门支撑轮', product_type: 'gate wheel', features: ['户外大门支撑'], uncertainties: [] };
const profile = { asin: 'B000000001', purpose: ['支撑大门'], style_structure: ['夹具与轮子'], specifications: ['户外使用'], price: '20', currency: 'USD', package_quantity: '1', unit_basis: 'each', price_source: 'asin_current' };
const roots = { project_id: project, run_id: runId, model: 'qwen3.8-max', marketplace: 'US', language: 'en', created_at: new Date().toISOString(), rule_version: 'product-term-v1', candidates: [{ id: candidate, text: 'gate wheel', translation: '大门轮', reason: '能搜索到同类商品', expansions: [] }], warnings: [] };
const analysis = { id: analysisId, project_id: project, run_id: runId, profile_version: 2, rule_version: 'product-term-v1', status: 'completed', complete: true, created_at: new Date().toISOString(), best_candidate_ids: [candidate], items: [{ candidate_id: candidate, text: 'gate wheel', status: 'completed', error: '', summary: { valid_samples: 5, high_related_count: 4, relevance_percent: '80', confidence: 'low' }, collection: { raw_natural_count: 6, unique_asins: ['B000000002'], products: [], failures: [], pages: [{ region: '10001', cards: [], next_page: false, url: 'https://www.amazon.com/s?k=gate+wheel', screenshot: 'x.png', diagnostic: 'x.json' }] }, comparisons: [{ asin: 'B000000002', status: 'high_related', purpose_score: 90, style_structure_score: 88, specifications_score: 84, price_score: null, overall_score: '87.33', high_related: true, purpose_reason: '用途一致', style_structure_reason: '结构一致', specifications_reason: '规格接近', price_reason: '包装数量不完整，无法归一单件价格', exclusion_reason: '', natural_position: 1, candidate_profile: { asin: 'B000000002', purpose: ['支撑大门'], style_structure: ['夹具与轮子'], specifications: ['户外使用'], price: '21', currency: 'USD', package_quantity: null, unit_basis: null, price_source: 'unknown' } }] }] };

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let analysisGets = 0;
    await page.route('**/api/v1/**', async route => {
      const url = new URL(route.request().url()).pathname;
      let data;
      if (url.endsWith(`/pipeline-runs/${runId}`)) data = { id: runId, project_id: project, status: 'input_received', input: { marketplace: 'US', asin: 'B000000001', product_name: null, description: '', image_ids: [], image_urls: [], reference_price: null } };
      else if (url.endsWith('/aba-reports')) data = [];
      else if (url.endsWith('/product-materials')) data = { schema_version: 1, project_id: project, run_id: runId, marketplace: 'US', asin: 'B000000001', supplied_product_name: null, supplied_description: '', reference_price: null, reference_currency: 'USD', images: [], asin_resolution: 'resolved', asin_product: null, model_status: 'succeeded', warnings: [] };
      else if (url.endsWith('/product-understanding')) data = { project_id: project, run_id: runId, provider: 'bailian', model: 'qwen3.8-max', created_at: new Date().toISOString(), product, warnings: [] };
      else if (url.endsWith('/product-confirmation/history')) data = [{ project_id: project, run_id: runId, version: 1, created_at: new Date().toISOString(), data: { expected_version: 0, product, reviewer: '运营', reason: '确认' } }];
      else if (url.endsWith('/product-profile')) data = { project_id: project, run_id: runId, version: 2, status: 'confirmed', profile, created_at: new Date().toISOString() };
      else if (url.endsWith('/root-candidates')) data = roots;
      else if (url.endsWith('/root-selection')) data = { project_id: project, run_id: runId, candidate_ids: [candidate], confirmed_at: new Date().toISOString(), source: 'automatic', verification_id: analysisId };
      else if (url.endsWith('/product-term-analyses/latest')) { analysisGets++; data = analysis; }
      else { await route.fulfill({ status: 404, json: { error: { message: 'offline fixture' } } }); return; }
      await route.fulfill({ status: 200, json: data });
    });
    await page.goto(`http://127.0.0.1:5173/?project=${project}&run=${runId}`);
    await page.getByText('系统推荐的最终商品词').waitFor();
    await page.getByText('gate wheel', { exact: true }).first().waitFor();
    await page.getByText('80.00%').waitFor();
    await page.getByText('低可信').first().waitFor();
    assert.equal(await page.locator('.comparison:visible').count(), 0, '逐商品证据默认应折叠');
    await page.locator('.candidate-card summary').click();
    await page.getByText('综合 87.33').waitFor();
    await page.getByText('价格未参与，按用途 / 款式 / 规格三维归一评分').waitFor();
    await page.screenshot({ path: process.argv[2], fullPage: true });
    await page.reload();
    await page.getByText('系统推荐的最终商品词').waitFor();
    assert.ok(analysisGets >= 2, '刷新应重新 GET 已有分析');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ finalTermVisible: true, evidenceCollapsed: true, lowConfidenceVisible: true, refreshRestored: true }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

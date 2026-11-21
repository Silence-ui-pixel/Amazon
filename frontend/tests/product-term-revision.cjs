// 离线浏览器回归：候选修改、版本历史、真实进度、门槛及刷新恢复。
const assert = require('node:assert/strict');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const project = '11111111-1111-4111-8111-111111111111';
const run = '22222222-2222-4222-8222-222222222222';
const ids = ['33333333-3333-4333-8333-333333333333', '44444444-4444-4444-8444-444444444444'];
const product = { product_name: '三层可调哑铃架', product_type: '哑铃收纳架', features: ['家庭健身'], uncertainties: [] };
const profile = { asin: 'B000000001', purpose: ['收纳哑铃'], style_structure: ['三层架'], specifications: ['可调节'], price: '89.99', currency: 'USD', package_quantity: '1', unit_basis: 'each', price_source: 'asin_current' };
const initialRoots = { project_id: project, run_id: run, version: 1, model: 'test', marketplace: 'US', language: 'en', rule_version: 'product-term-v1', created_at: new Date().toISOString(), candidates: ids.map((id, i) => ({ id, text: ['rack', 'fitness'][i], translation: ['架', '健身'][i], reason: '旧候选' })), warnings: [] };
const summary = (high) => ({ valid_samples: 30, high_related_count: high, relevance_percent: (high / 30 * 100).toFixed(2), confidence: 'high' });
const oldAnalysis = { id: 'old', candidate_version: 1, status: 'awaiting_review', complete: true, best_candidate_ids: [], items: initialRoots.candidates.map(c => ({ candidate_id: c.id, text: c.text, status: 'completed', summary: summary(0), comparisons: [], collection: null, error: '', rejection_reason: '未严格大于40%' })) };

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: 'C:/Users/Administrator/AppData/Local/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-win64/chrome-headless-shell.exe' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    let roots = structuredClone(initialRoots);
    let stage = 'old';
    let writes = 0;
    let evidenceReads = 0;
    const newItems = () => roots.candidates.map((c, i) => ({ candidate_id: c.id, text: c.text, status: i ? 'waiting' : 'running', phase: 'collecting', search_pages: i ? 0 : 2, found_products: i ? 0 : 30, collected_products: i ? 0 : 6, collection_failures: 0, scored_products: 0, summary: null, comparisons: [], collection: null, error: '' }));
    const analysis = () => stage === 'old' ? oldAnalysis : stage === 'running' ? { id: 'new', candidate_version: 2, status: 'running', complete: false, best_candidate_ids: [], items: newItems() } : { id: 'new', candidate_version: 2, status: 'completed', complete: true, best_candidate_ids: [roots.candidates[0].id], items: newItems().map((item, i) => ({ ...item, status: 'completed', phase: 'completed', evidence_loaded: false, summary: summary(i ? 9 : 14), rejection_reason: i ? '高相关比例30%，未严格大于40%' : '' })) };
    await page.route('**/api/v1/**', async route => {
      const req = route.request();
      const url = new URL(req.url()).pathname;
      let data;
      if (url.endsWith(`/pipeline-runs/${run}`)) data = { id: run, project_id: project, status: 'input_received', input: { marketplace: 'US', asin: 'B000000001', description: '', image_ids: [], image_urls: [], reference_price: null } };
      else if (url.endsWith('/aba-reports')) data = [];
      else if (url.endsWith('/product-materials')) data = { schema_version: 1, project_id: project, run_id: run, marketplace: 'US', asin: 'B000000001', supplied_description: '', images: [], asin_resolution: 'resolved', asin_product: null, model_status: 'succeeded', warnings: [] };
      else if (url.endsWith('/product-understanding')) data = { project_id: project, run_id: run, provider: 'bailian', model: 'test', created_at: new Date().toISOString(), product, warnings: [] };
      else if (url.endsWith('/product-confirmation/history')) data = [{ version: 1, created_at: new Date().toISOString(), data: { expected_version: 0, product, reviewer: '运营', reason: '确认' } }];
      else if (url.endsWith('/product-profile')) data = { project_id: project, run_id: run, version: 2, status: 'confirmed', profile, created_at: new Date().toISOString() };
      else if (url.endsWith('/root-candidates')) data = roots;
      else if (url.endsWith('/product-term-candidates') && req.method() === 'PUT') {
        const body = req.postDataJSON();
        assert.equal(body.expected_version, 1);
        assert.deepEqual(body.candidates.map(c => c.text), ['dumbbell rack', 'dumbbell stand']);
        writes++;
        roots = { ...initialRoots, version: 2, source: 'manual', history: [initialRoots], candidates: body.candidates.map((c, i) => ({ ...c, id: `new-${i}` })) };
        data = roots;
      } else if (url.endsWith('/product-term-analyses') && req.method() === 'POST') { stage = 'running'; data = { ...analysis(), status: 'queued' }; }
      else if (url.endsWith('/product-term-analyses/latest')) data = analysis();
      else if (url.endsWith('/product-term-analyses/history')) data = [oldAnalysis];
      else if (url.endsWith('/product-term-analyses/new/candidates/new-0')) { evidenceReads++; data = { ...analysis().items[0], evidence_loaded: true, comparisons: [{ asin: 'B000000010', status: 'high_related', overall_score: '88', natural_position: 1, price_score: null, purpose_reason: '用途一致', style_structure_reason: '结构一致', specifications_reason: '规格接近', price_reason: '', exclusion_reason: '', candidate_profile: null }] }; }
      else if (url.endsWith('/root-selection') && stage === 'done') data = { candidate_ids: [roots.candidates[0].id], source: 'automatic', confirmed_at: new Date().toISOString() };
      else { await route.fulfill({ status: 404, json: { error: { message: 'offline fixture' } } }); return; }
      await route.fulfill({ status: 200, json: data });
    });
    await page.goto(`http://127.0.0.1:5174/?project=${project}&run=${run}`);
    await page.getByRole('button', { name: '修改 / 补充商品词' }).waitFor();
    await page.getByText('等待补充商品词', { exact: true }).waitFor();
    await page.getByRole('button', { name: '修改 / 补充商品词' }).click();
    await page.getByRole('textbox', { name: '商品词 1', exact: true }).fill('dumbbell rack');
    await page.getByRole('textbox', { name: '中文翻译 1', exact: true }).fill('哑铃架');
    await page.getByRole('textbox', { name: '商品词 2', exact: true }).fill('dumbbell stand');
    await page.getByRole('textbox', { name: '中文翻译 2', exact: true }).fill('哑铃支架');
    await page.getByRole('button', { name: '保存为新版本并验证' }).click();
    await page.getByText('候选商品词 · 第 2 版', { exact: true }).waitFor();
    await page.getByText('采集商品详情 · 详情 6/30 · 评分 0/6', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '修改 / 补充商品词' }).isDisabled(), true);
    assert.equal(await page.getByText('等待处理', { exact: true }).count(), 1);
    stage = 'done';
    await page.getByText('46.67%', { exact: true }).waitFor();
    assert.equal(await page.locator('.candidate-card input').nth(1).isDisabled(), true);
    await page.locator('.candidate-card summary').first().click();
    await page.getByText('综合 88.00', { exact: true }).waitFor();
    assert.equal(evidenceReads, 1);
    await page.getByText('历史候选与分析记录', { exact: true }).click();
    await page.getByText('第 1 版：rack、fitness', { exact: true }).waitFor();
    await page.screenshot({ path: process.argv[2] || 'outputs/candidate-revision.png', fullPage: true });
    await page.reload();
    await page.getByText('候选商品词 · 第 2 版', { exact: true }).waitFor();
    await page.getByText('46.67%', { exact: true }).waitFor();
    assert.equal(writes, 1, '刷新不得重新提交修改或生成模型请求');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ edit: true, versionHistory: true, collectionProgress: true, pendingLabels: true, thresholdPreserved: true, lazyEvidence: true, refreshReadOnly: true }));
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });

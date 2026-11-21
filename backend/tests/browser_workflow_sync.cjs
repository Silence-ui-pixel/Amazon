// Isolated browser contract test: all API traffic is fulfilled locally.
const assert = require('node:assert/strict');
const { chromium, request } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const run = { id: 'sync-run', project_id: 'sync-project', status: 'input_received' };
const product = { product_name: '原始商品', product_type: 'rack', features: [], uncertainties: [] };
const confirmation = version => ({ version, created_at: '2026-10-27', data: { expected_version: version - 1, product: { ...product, product_name: `后台商品${version}` }, reviewer: '审核员', reason: '已核对' } });
const workflow = revision => ({ workflow_id: 'flow', run_id: run.id, project_id: run.project_id, status: 'waiting_human', revision, current_node: 'product_confirmation', nodes: [{ key: 'product_confirmation', label: '商品确认', status: 'waiting_human', detail: '' }], waiting_for: { kind: 'human', gate: 'product_confirmation', title: '确认商品', instructions: '', actions: ['continue'], refs: {}, ready_to_resume: false }, error: '', next_poll_ms: 1000, created_at: '2026-10-27', updated_at: '2026-10-27' });
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const errors = [], writes = [];
    let revision = 1, version = 1, reads = 0;
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/__sync-test', route => route.fulfill({ contentType: 'text/html', body: '<div id="test"></div>' }));
    await page.route('**/api/v1/**', async route => {
      const req = route.request(), path = new URL(req.url()).pathname;
      if (req.method() !== 'GET') writes.push({ path, body: req.postDataJSON() });
      if (path.endsWith('/workflow')) return route.fulfill({ json: workflow(revision) });
      if (path.endsWith('/resume')) return route.fulfill({ json: { ...workflow(++revision), status: 'completed', current_node: null, waiting_for: null } });
      if (path.endsWith('/product-confirmation/history')) { reads++; return route.fulfill({ json: [confirmation(version)] }); }
      if (path.endsWith('/product-confirmation') && req.method() === 'PUT') return route.fulfill({ json: { ...confirmation(++version), data: req.postDataJSON() } });
      return route.fulfill({ status: 404, json: { detail: 'absent in isolated test' } });
    });
    await page.goto('http://127.0.0.1:5173/__sync-test');
    await page.evaluate(async ({ run, product }) => {
      const { createApp, h, reactive } = await import('/node_modules/.vite/deps/vue.js');
      const Workflow = (await import('/src/components/WorkflowBar.vue')).default;
      const Product = (await import('/src/components/ProductConfirmationPanel.vue')).default;
      window.state = reactive({ revision: 0, savedAction: undefined, views: 0 });
      window.app = createApp({ render: () => h('div', [
        h(Workflow, { run, reportWeek: '2026-10-24', savedAction: window.state.savedAction, onView: () => { window.state.views++; window.state.revision++; } }),
        h(Product, { run, product, locked: false, syncRevision: window.state.revision, onSaved: () => { window.state.savedAction = { id: 1, gate: 'product_confirmation' }; } }),
      ]) });
      window.app.mount('#test');
    }, { run, product });
    const name = page.getByLabel('确认商品名称', { exact: true });
    await name.waitFor();
    await page.waitForFunction(() => document.querySelector('[aria-label="确认商品名称"]').value === '后台商品1');
    const initialReads = reads;
    await page.waitForTimeout(1200);
    assert.equal(await page.evaluate(() => window.state.views), 1, 'identical workflow poll must not broadcast');
    assert.equal(reads, initialReads, 'no feedback reads');
    assert.equal(writes.length, 0, 'reads must never resume a human gate');
    version = 2; revision = 2;
    await page.waitForFunction(() => document.querySelector('[aria-label="确认商品名称"]').value === '后台商品2');
    await name.fill('我的未保存草稿');
    version = 3; revision = 3;
    await page.getByRole('status').waitFor();
    assert.equal(await name.inputValue(), '我的未保存草稿');
    assert.equal(writes.length, 0);
    await page.getByRole('button', { name: '读取确认记录（丢弃修改）', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[aria-label="确认商品名称"]').value === '后台商品3');
    await name.fill('人工保存商品');
    await page.getByRole('button', { name: '确认商品资料', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.pill')?.textContent.trim() === '已完成');
    assert.equal(writes.filter(w => w.path.endsWith('/resume')).length, 1, 'one save advances once');
    // A historical failed cleaning workflow offers a human-only recovery start.
    await page.route('**/pipeline-runs/sync-run/workflow', route => {
      if (route.request().method() === 'POST') {
        const body = route.request().postDataJSON();
        assert.equal(body.options.manual_cleaning_review, true);
        return route.fulfill({ json: { ...workflow(20), current_node: 'cleaning_review', nodes: [{ key: 'cleaning_review', label: '清洗审核', status: 'waiting_human', detail: '' }], waiting_for: { ...workflow(20).waiting_for, gate: 'cleaning_review', title: '人工审核清洗' } } });
      }
      return route.fulfill({ json: { ...workflow(19), status: 'failed', current_node: null, waiting_for: null, error: 'MODEL_OUTPUT_INVALID: 模型未返回有效的清洗审核结果', nodes: [{ key: 'cleaning_review', label: '清洗审核', status: 'failed', detail: '' }] } });
    });
    await page.evaluate(async run => {
      const { createApp, h } = await import('/node_modules/.vite/deps/vue.js');
      const Workflow = (await import('/src/components/WorkflowBar.vue')).default;
      window.app.unmount();
      window.app = createApp({ render: () => h(Workflow, { run, reportWeek: '2026-10-24' }) });
      window.app.mount('#test');
    }, run);
    await page.getByRole('button', { name: '改为人工审核清洗，保留已有结果', exact: true }).click();
    await page.getByText('人工审核清洗', { exact: true }).waitFor();
    console.log('cleaning model failure: human-only recovery entry passed');
    let trafficRetries = 0;
    await page.route('**/pipeline-runs/sync-run/workflow', route => route.fulfill({ json: {
      ...workflow(21), status: 'failed', current_node: null, waiting_for: null,
      error: 'TRAFFIC_VERSION_CONFLICT: 审核已更新',
      nodes: [{ key: 'traffic_review', label: '流量审核', status: 'failed', detail: '' }],
    } }));
    await page.route('**/workflow/flow/retry-traffic', route => {
      trafficRetries++;
      assert.deepEqual(route.request().postDataJSON(), { expected_revision: 21, gate: 'traffic_review', action: 'continue' });
      return route.fulfill({ json: { ...workflow(21), status: 'running', current_node: 'traffic_review', waiting_for: null } });
    });
    await page.evaluate(async run => {
      const { createApp, h } = await import('/node_modules/.vite/deps/vue.js');
      const Workflow = (await import('/src/components/WorkflowBar.vue')).default;
      window.app.unmount();
      window.app = createApp({ render: () => h(Workflow, { run, reportWeek: '2026-10-24' }) });
      window.app.mount('#test');
    }, run);
    await page.getByRole('button', { name: '继续剩余流量审核', exact: true }).click();
    await page.getByText('执行中', { exact: true }).waitFor();
    assert.equal(trafficRetries, 1);
    console.log('traffic recovery: remaining reviews entry passed');
    let coreRestarts = 0;
    await page.route('**/pipeline-runs/sync-run/workflow', route => {
      if (route.request().method() === 'POST') {
        const body = route.request().postDataJSON();
        assert.equal(body.options.reanalyze_core, true);
        assert.equal(body.options.auto_decide, true);
        coreRestarts++;
        return route.fulfill({ json: { ...workflow(22), status: 'running', current_node: 'core_terms', waiting_for: null } });
      }
      return route.fulfill({ json: { ...workflow(21), status: 'completed', waiting_for: null } });
    });
    await page.evaluate(async run => {
      const { createApp, h } = await import('/node_modules/.vite/deps/vue.js');
      const Workflow = (await import('/src/components/WorkflowBar.vue')).default;
      window.app.unmount();
      window.app = createApp({ render: () => h(Workflow, { run, reportWeek: '2026-10-24' }) });
      window.app.mount('#test');
    }, run);
    await page.getByRole('button', { name: '从核心大词重新分析', exact: true }).click();
    await page.getByText('执行中', { exact: true }).waitFor();
    assert.equal(coreRestarts, 1);
    console.log('core reanalysis: explicit restart passed');
    await page.evaluate(async run => {
      const { createApp, h } = await import('/node_modules/.vite/deps/vue.js');
      const Batch = (await import('/src/components/BatchTrafficPanel.vue')).default;
      window.app.unmount();
      window.batchDirty = false;
      window.app = createApp({ render: () => h(Batch, { run, report: { id: 'traffic', version: 1, stale: false }, rows: [], disabled: false, reset: 0, onDirty: value => { window.batchDirty = value; } }) });
      window.app.mount('#test');
    }, run);
    await page.locator('.batch-traffic summary').click();
    await page.getByLabel('批量审核理由', { exact: true }).fill('尚未勾词的批量草稿');
    assert.equal(await page.evaluate(() => window.batchDirty), true, 'text without selected keywords is a draft');
    assert.deepEqual(errors, []);
    await page.unrouteAll({ behavior: 'wait' });
    // Full App integration, replaying existing read-only business records.
    await page.close();
    const appPage = await browser.newPage();
    const replayApi = await request.newContext();
    const realRun = { id: '0d77aca7-c4b3-4334-a667-8628913095bf', project_id: 'a6a55f2c-2195-4fd7-be69-c6ed25ca17bc', status: 'input_received' };
    let stage = 0;
    const appWrites = [], appErrors = [], cache = new Map();
    appPage.on('pageerror', e => appErrors.push(e.message));
    await appPage.addInitScript(run => localStorage.setItem('keyword-library-active-run-v1', JSON.stringify({ run, marketplace: 'US', asin: 'B0CGV33WDY', root: '', product: '', reportWeek: '2026-10-17' })), realRun);
    await appPage.route('**/api/v1/**', async route => {
      const req = route.request(), url = new URL(req.url());
      if (req.method() !== 'GET') { appWrites.push(url.pathname); return route.fulfill({ status: 409, json: { detail: 'writes forbidden in replay' } }); }
      if (url.pathname.endsWith('/workflow')) return route.fulfill({ json: { ...workflow(stage + 1), run_id: realRun.id, project_id: realRun.project_id } });
      if (url.pathname.endsWith('/root-selection') && stage === 0) return route.fulfill({ status: 404, json: { detail: 'not produced yet' } });
      const key = url.pathname + url.search;
      if (!cache.has(key)) {
        const res = await replayApi.get('http://127.0.0.1:8000' + key, { timeout: 15000 });
        cache.set(key, { status: res.status(), body: await res.body(), contentType: res.headers()['content-type'] || 'application/json' });
      }
      return route.fulfill(cache.get(key)).catch(() => {});
    });
    console.log('full app replay starting');
    await appPage.goto('http://127.0.0.1:5173/', { waitUntil: 'domcontentloaded' });
    await appPage.locator('.workflow-bar').waitFor({ timeout: 10000 });
    console.log('workflow bar restored');
    assert.equal(await appPage.locator('.core-panel').count(), 0);
    console.log('stage 1');
    stage = 1;
    try { await appPage.locator('.core-panel').waitFor({ state: 'attached', timeout: 12000 }); } catch (e) { console.log(await appPage.locator('body').innerText()); throw e; }
    console.log('core restored');
    await appPage.locator('.cleaning-panel').waitFor({ state: 'attached' });
    console.log('cleaning restored');
    await appPage.locator('.traffic-panel').waitFor({ state: 'attached' });
    console.log('traffic restored');
    await appPage.locator('.library-panel').waitFor({ state: 'attached' });
    console.log('library restored');
    await appPage.waitForTimeout(1800);
    console.log('full replay settled');
    const nav = appPage.getByRole('navigation', { name: '八步操作导航' });
    assert.equal(await nav.locator('button').count(), 8);
    await nav.locator('[data-step="1"]').click();
    const brands = appPage.getByLabel('自家品牌（选填）', { exact: true });
    await brands.fill('导航保留测试草稿');
    await nav.locator('[data-step="2"]').click();
    assert.equal(await brands.isVisible(), false);
    await nav.locator('[data-step="1"]').click();
    assert.equal(await brands.inputValue(), '导航保留测试草稿');
    assert.equal(await appPage.locator('.workspace-pane:visible').count(), 1);
    await nav.locator('[data-step="5"]').click();
    const cleaning = appPage.locator('.cleaning-panel');
    await cleaning.getByRole('checkbox').first().check();
    assert.equal(await cleaning.getByRole('button', { name: '生成词频分析', exact: true }).isDisabled(), true, 'incomplete source routes block cleaning');
    await nav.locator('[data-step="4"]').click();
    assert.equal(await appPage.locator('.manual-import').getAttribute('open'), null);
    assert.equal(await appPage.locator('.execution-details').getAttribute('open'), null);
    await appPage.setViewportSize({ width: 1440, height: 1050 });
    await appPage.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
    await appPage.screenshot({ path: 'outputs/workspace-guided-desktop.png' });
    await appPage.setViewportSize({ width: 390, height: 844 });
    for (let i = 0; i < 8; i++) {
      await nav.locator(`[data-step="${i}"]`).click();
      assert.equal(await appPage.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `mobile step ${i+1} overflow`);
    }
    await appPage.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
    await appPage.screenshot({ path: 'outputs/workspace-guided-mobile.png' });
    await appPage.goto('http://127.0.0.1:5173/#competitors-title', { waitUntil: 'domcontentloaded' });
    await appPage.waitForFunction(() => document.querySelector('.step-nav-item[data-step="2"]')?.getAttribute('aria-current') === 'step');
    console.log('competitor deep link opens correct step');
    console.log('guided workspace: eight steps, draft retention, collapsed details, mobile passed');
    assert.deepEqual(appWrites, [], 'full App background synchronization must remain GET-only');
    assert.deepEqual(appErrors, []);
    console.log('clearing replay routes');
    await appPage.unrouteAll({ behavior: 'ignoreErrors' });
    await replayApi.dispose();
    const fresh = await browser.newPage();
    const freshWrites = [];
    await fresh.route('**/api/v1/**', route => {
      if (route.request().method() !== 'GET') freshWrites.push(route.request().url());
      return route.fulfill({ status: 404, json: { detail: 'no saved task' } });
    });
    await fresh.goto('http://127.0.0.1:5173/');
    const freshNav = fresh.getByRole('navigation', { name: '八步操作导航' });
    await fresh.getByLabel('商品 ASIN', { exact: true }).fill('B0CGV33WDY');
    await freshNav.locator('[data-step="5"]').click();
    assert.match(await fresh.locator('.step-blocked').innerText(), /两|ABA/);
    assert.equal(await fresh.getByRole('button', { name: '生成词频分析', exact: true }).count(), 0);
    await freshNav.locator('[data-step="0"]').click();
    assert.equal(await fresh.getByLabel('商品 ASIN', { exact: true }).inputValue(), 'B0CGV33WDY');
    assert.deepEqual(freshWrites, []);
    await fresh.close();
    console.log(JSON.stringify({ fullAppUnlocksPanels: true, replayWithoutWrites: true }));
    console.log(JSON.stringify({ workflowDedupe: true, backgroundRefresh: true, draftProtected: true, readOnlyRefresh: true, saveResumesOnce: true, errors }));
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });


